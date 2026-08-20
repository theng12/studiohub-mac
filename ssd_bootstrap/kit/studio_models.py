#!/usr/bin/env python3
"""Stage every Studio model cache and fleet voice onto an SSD, then restore
RAM-qualified payloads on each fleet Mac in one pass.

    # on the machine that already has the models (plug the SSD in first)
    python3 tools/studio_models.py stage --plan
    python3 tools/studio_models.py stage

    # on each fleet Mac
    python3 tools/studio_models.py restore --plan
    python3 tools/studio_models.py restore
    python3 tools/studio_models.py restore --prune     # also reclaim dead weight

Why this exists as one tool rather than one per studio: a Mac needs its voice
*and* image models, the trip to that Mac is the expensive part, and the two
caches are otherwise identical in shape. One script, one SSD folder, one run.

Three things it gets right that a naive copy does not:

1. **Symlinks are preserved.** The Hugging Face cache stores each real file once
   under `blobs/` and points at it from `snapshots/` with a symlink. A plain
   copy dereferences those and writes every file twice -- measured on the voice
   cache, 52 GB became 98 GB. Copying to a filesystem that cannot store symlinks
   at all (exFAT/FAT32) aborts rather than silently doubling.

2. **Nothing is hardcoded to one machine's layout.** Each studio is asked where
   its own cache lives, because the fleet does not match this machine: here the
   image launcher is `imagestudio-mac`, on the fleet it is `imagestudio-mac.git`,
   and the home directory differs too. A path constant would have been wrong on
   every Mac it was carried to.

3. **A stale local catalogue cannot cause a wrong delete.** Staging is additive,
   and memory floors are
   resolved as the highest value any source knows, because a machine running an
   older studio reports a floor of `None` for a model that has since been
   measured. Taking that at face value would have deleted a perfectly good Fish
   Audio cache off a 16 GB worker.

Pruning is explicit and only removes a model whose floor is *known* and higher
than this Mac's memory -- something this machine physically cannot run.
Companions (codecs, tokenizers) are never pruned; they are small and a model
without its codec is useless. Shared voices are never pruned.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

STUDIOS = {
    "voice": {"port": 47870, "label": "Voice Studio"},
    "image": {"port": 47868, "label": "Image Studio"},
}

MANIFEST_NAME = "MANIFEST.json"
STT_FAMILY = "Whisper (speech-to-text)"
QWEN_06B_BASE = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit"
QWEN_QUALITY_WHISPER = "mlx-community/whisper-large-v3-turbo"
EIGHT_GB_VOICE_ALLOWLIST = frozenset({QWEN_06B_BASE, QWEN_QUALITY_WHISPER})
PREFERRED_VOLUME_NAMES = ("ugreen-terranash", "UGREEN-1TB")

def needed_companions(models: list[dict]) -> set[str]:
    """Return every companion used by a local catalog model."""
    needed: set[str] = set()
    for m in models:
        if not is_local(m):
            continue
        for c in (m.get("cache") or {}).get("companions") or []:
            needed.add(c["repo"])
    return needed


# ---------------------------------------------------------------- discovery

def api(port: int, path: str, timeout: float = 25.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def dirname_to_repo(dirname: str) -> str | None:
    if not dirname.startswith("models--"):
        return None
    parts = dirname.removeprefix("models--").split("--")
    return f"{parts[0]}/{'--'.join(parts[1:])}" if len(parts) >= 2 else None


def discover(port: int) -> tuple[Path | None, list[dict]]:
    """Ask a studio where its cache lives and what is in its catalogue.

    The hub path is read back from a model's own cache entry rather than
    assembled from a launcher-folder guess, so it stays correct across machines
    whose folder names and usernames differ.
    """
    catalog = api(port, "/api/catalog")
    if catalog is None:
        return None, []
    models = catalog.get("models", [])
    hub = None
    for m in models:
        p = (m.get("cache") or {}).get("path")
        if p and "models--" in p:
            hub = Path(p).parent
            break
    return hub, models


def discover_fleet_voices(port: int = STUDIOS["voice"]["port"]) -> list[dict]:
    payload = api(port, "/api/voices", timeout=15) or {}
    voices = payload.get("voices")
    if not isinstance(voices, list):
        return []
    return [voice for voice in voices
            if isinstance(voice, dict) and voice.get("fleet_managed") is True]


def download_voice_audio(port: int, voice_id: str) -> bytes:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/voices/{voice_id}/audio", timeout=60
    ) as response:
        return response.read(25_000_001)


def installed_hub(pinokio_home: Path, studio: str) -> Path | None:
    """Resolve an installed Studio's HF hub without starting its server."""
    candidates = (
        pinokio_home / "api" / studio,
        pinokio_home / "api" / f"{studio}.git",
    )
    target = next((path for path in candidates if path.is_dir()), None)
    if target is None:
        return None
    hf_home = None
    try:
        for line in (target / "ENVIRONMENT").read_text().splitlines():
            if line.strip().startswith("HF_HOME="):
                hf_home = line.split("=", 1)[1].strip().strip("\"'")
                break
    except OSError:
        pass
    path = Path(hf_home).expanduser() if hf_home else Path("cache/HF_HOME")
    if not path.is_absolute():
        path = target / path
    return path.resolve() / "hub"


def installed_studio(pinokio_home: Path, studio: str) -> Path | None:
    for candidate in (
        pinokio_home / "api" / studio,
        pinokio_home / "api" / f"{studio}.git",
    ):
        if candidate.is_dir():
            return candidate
    return None


def staleness(port: int, hub: Path) -> str | None:
    """Warn when a studio is serving an older build than its checked-out code.

    Staging reads memory floors from the *running* server, so a studio that has
    not been restarted since its catalogue changed bakes stale floors into the
    manifest and ships them to every machine. Caught in practice: Fish Audio's
    floor was corrected to 16 GB on disk while the running server still reported
    `None`, which would have marked it unqualified fleet-wide.

    The repo root is the hub's great-grandparent (`<repo>/cache/HF_HOME/hub`).
    """
    served = (api(port, "/api/version", timeout=8) or {}).get("app_version")
    version_file = hub.parent.parent.parent / "VERSION"
    try:
        on_disk = version_file.read_text().strip()
    except OSError:
        return None
    if served and on_disk and served != on_disk:
        return (f"serving {served} but the code on disk is {on_disk} — restart it "
                "before staging, or the manifest carries stale memory floors")
    return None


def machine_memory_gb() -> float:
    out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                         capture_output=True, text=True)
    return round(int(out.stdout.strip() or 0) / 1e9, 1)


def dir_bytes(p: Path) -> int:
    """Apparent size WITHOUT following symlinks -- what will actually be written."""
    out = subprocess.run(["du", "-sk", str(p)], capture_output=True, text=True)
    return int(out.stdout.split()[0]) * 1024 if out.returncode == 0 else 0


def supports_symlinks(path: Path) -> bool:
    probe, target = path / ".symlink_probe", path / ".symlink_target"
    try:
        target.write_text("x")
        if probe.exists() or probe.is_symlink():
            probe.unlink()
        probe.symlink_to(target)
        return probe.is_symlink()
    except (OSError, NotImplementedError):
        return False
    finally:
        for p in (probe, target):
            try:
                p.unlink()
            except OSError:
                pass


def find_default_root(volumes_root: Path = Path("/Volumes")) -> Path:
    """Find this fleet SSD without making its display name an API contract."""
    configured = os.environ.get("TERRANASH_SSD_ROOT", "").strip()
    if configured:
        value = Path(configured).expanduser()
        return value if value.name == "studio-models" else value / "studio-models"
    for name in PREFERRED_VOLUME_NAMES:
        candidate = volumes_root / name / "studio-models"
        if candidate.joinpath(MANIFEST_NAME).is_file() or candidate.parent.is_dir():
            return candidate
    try:
        mounted = list(volumes_root.iterdir())
    except OSError:
        mounted = []
    matches = [volume / "studio-models" for volume in mounted
               if volume.joinpath("studio-models", MANIFEST_NAME).is_file()]
    if len(matches) == 1:
        return matches[0]
    return volumes_root / PREFERRED_VOLUME_NAMES[0] / "studio-models"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def package_signature(
    root: Path, *, omit_zero_incomplete: bool = False
) -> tuple[tuple[str, str, int | str], ...]:
    """Cheap structural identity for content-addressed HF cache packages."""
    rows: list[tuple[str, str, int | str]] = []
    for path in sorted(root.rglob("*")):
        if (
            omit_zero_incomplete
            and path.name.endswith(".incomplete")
            and path.is_file()
            and path.stat().st_size == 0
        ):
            continue
        relative = str(path.relative_to(root))
        if path.is_symlink():
            rows.append((relative, "link", os.readlink(path)))
        elif path.is_dir():
            rows.append((relative, "dir", 0))
        else:
            rows.append((relative, "file", path.stat().st_size))
    return tuple(rows)


def copy_package_if_changed(source: Path, destination: Path) -> str:
    """Atomically add or refresh one package without recopying identical data."""
    source_signature = package_signature(source, omit_zero_incomplete=True)
    if destination.is_dir() and source_signature == package_signature(destination):
        return "intact"
    action = "replace" if destination.exists() else "copy"
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    temporary = destination.parent / f".{destination.name}.{token}.staging"
    backup = destination.parent / f".{destination.name}.{token}.backup"
    try:
        def ignore_zero_incomplete(directory: str, names: list[str]) -> set[str]:
            base = Path(directory)
            return {
                name for name in names
                if name.endswith(".incomplete")
                and (base / name).is_file()
                and (base / name).stat().st_size == 0
            }

        shutil.copytree(
            source, temporary, symlinks=True, ignore=ignore_zero_incomplete
        )
        if destination.exists():
            destination.replace(backup)
        try:
            temporary.replace(destination)
        except Exception:
            if backup.exists() and not destination.exists():
                backup.replace(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        if backup.exists() and destination.exists():
            shutil.rmtree(backup)
    return action


def _voice_reference(directory: Path, metadata: dict) -> Path | None:
    extension = str(metadata.get("audio_extension") or "")
    expected = directory / f"reference{extension}"
    if extension and expected.is_file():
        return expected
    return next((path for path in sorted(directory.glob("reference.*")) if path.is_file()), None)


def validate_voice_entry(entry: dict) -> tuple[str, str]:
    voice_id = str(entry.get("id") or "")
    digest = str(entry.get("audio_sha256") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", voice_id):
        raise ValueError("voice ID is not safe for portable storage")
    if str(entry.get("dir") or voice_id) != voice_id:
        raise ValueError("voice directory must match its stable ID")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"voice {voice_id} has no stable SHA-256")
    return voice_id, digest


def restore_voice(source: Path, voices_dir: Path, entry: dict) -> str:
    """Restore a stable fleet voice without ever overwriting a conflicting ID."""
    voice_id, expected = validate_voice_entry(entry)
    source_meta = json.loads((source / "metadata.json").read_text())
    source_audio = _voice_reference(source, source_meta)
    if source_audio is None or file_sha256(source_audio) != expected:
        raise ValueError(f"staged voice {entry.get('id')} failed its SHA-256 check")
    destination = voices_dir / voice_id
    if destination.exists():
        try:
            current_meta = json.loads((destination / "metadata.json").read_text())
        except (OSError, ValueError):
            return "conflict"
        current_audio = _voice_reference(destination, current_meta)
        if (
            current_meta.get("fleet_managed") is True
            and current_audio is not None
            and file_sha256(current_audio) == expected
        ):
            return "intact"
        return "conflict"
    copy_package_if_changed(source, destination)
    return "copy"


def stage_voice(root: Path, voice: dict) -> tuple[str, dict]:
    """Add one fleet-managed Voice Studio reference to the SSD."""
    voice_id = str(voice.get("id") or "")
    digest = str(voice.get("audio_sha256") or "")
    extension = str(voice.get("audio_extension") or "")
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", extension):
        raise ValueError(f"voice {voice_id} has an unsafe audio extension")

    entry = {
        "id": voice_id,
        "dir": voice_id,
        "name": str(voice.get("name") or voice_id),
        "audio_extension": extension,
        "audio_sha256": digest,
    }
    validate_voice_entry(entry)
    destination = root / "voices" / voice_id
    if destination.is_dir():
        try:
            metadata = json.loads((destination / "metadata.json").read_text())
        except (OSError, ValueError):
            return "conflict", entry
        reference = _voice_reference(destination, metadata)
        if reference is not None and file_sha256(reference) == digest:
            return "intact", entry
        return "conflict", entry

    audio = download_voice_audio(STUDIOS["voice"]["port"], voice_id)
    if len(audio) > 25_000_000 or bytes_sha256(audio) != digest:
        raise ValueError(f"voice {voice_id} download failed its SHA-256 check")
    temporary_root = root / f".voice-{uuid.uuid4().hex}.staging"
    source = temporary_root / voice_id
    try:
        source.mkdir(parents=True)
        metadata = {
            key: value for key, value in voice.items()
            if key not in {"audio_url", "audio_filename", "transcript"}
        }
        (source / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        (source / f"reference{extension}").write_bytes(audio)
        transcript = str(voice.get("transcript") or "").strip()
        if transcript:
            (source / "transcript.txt").write_text(transcript + "\n")
        action = copy_package_if_changed(source, destination)
    finally:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)
    return action, entry


def make_portable_readable(root: Path) -> None:
    """Add read/traverse access without changing ownership or following links.

    SSD payloads are staged by one macOS account but consumed by another. Copy
    tools preserve restrictive source modes, so a cache created under umask 077
    would otherwise be unreadable even though every path is location-relative.
    """
    root.chmod(root.stat().st_mode | 0o555)
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        access = 0o555 if path.is_dir() else 0o444
        path.chmod(path.stat().st_mode | access)


def stale_staged_packages(root: Path, wanted: set[Path]) -> list[Path]:
    """Return obsolete HF packages inside this tool's owned SSD layout only."""
    stale: list[Path] = []
    for studio in STUDIOS:
        studio_root = root / studio
        if not studio_root.is_dir():
            continue
        for family in studio_root.iterdir():
            if not family.is_dir() or family.is_symlink():
                continue
            for package in family.iterdir():
                if (package.is_dir() and not package.is_symlink()
                        and package.name.startswith("models--")
                        and package.relative_to(root) not in wanted):
                    stale.append(package)
    return sorted(stale)


def is_local(model: dict) -> bool:
    """Cloud entries have no weights to move."""
    return model.get("provider") != "cloud" and model.get("kind") != "cloud"


def catalog_index(models: list[dict]) -> tuple[dict, dict, set]:
    """repo -> family label, repo -> floor, and the set of companion repos."""
    families, floors, companions = {}, {}, set()
    for m in models:
        if not is_local(m):
            continue
        fam = m.get("family_label") or m.get("family") or "Other"
        families[m["repo"]] = fam
        floors[m["repo"]] = m.get("min_unified_memory_gb")
        for c in (m.get("cache") or {}).get("companions") or []:
            families.setdefault(c["repo"], fam)
            companions.add(c["repo"])
            # A companion rides with the least demanding model that needs it.
            prev = floors.get(c["repo"])
            cur = m.get("min_unified_memory_gb")
            if prev is None or (cur is not None and cur < prev):
                floors.setdefault(c["repo"], cur)
    return families, floors, companions


def catalog_cache_states(models: list[dict]) -> dict[str, str]:
    states: dict[str, str] = {}
    for model in models:
        cache_info = model.get("cache") or {}
        state = cache_info.get("state")
        if state:
            states[model["repo"]] = state
        for companion in cache_info.get("companions") or []:
            repo = companion.get("repo")
            companion_state = companion.get("state")
            if repo and companion_state:
                if states.get(repo) != "cached" or companion_state == "cached":
                    states[repo] = companion_state
    return states


def resolve_floor(repo: str, *sources: dict) -> float | None:
    """Highest floor any source knows.

    A studio that predates a model's qualification reports None. Treating that
    as "no requirement" is how a 16 GB-only model looks prunable on an 8 GB Mac
    -- or worse, looks prunable on the 16 GB Mac that can actually run it.
    """
    known = [s[repo] for s in sources if s.get(repo) is not None]
    return max(known) if known else None


def restore_floor(studio: str, repo: str, ram: float, floor: float | None) -> float | None:
    if studio == "voice" and ram < 12 and repo == QWEN_06B_BASE:
        return 8.0
    return floor


def restore_allowed(studio: str, repo: str, ram: float, restore_all: bool) -> bool:
    if restore_all or studio != "voice" or ram >= 12:
        return True
    return repo in EIGHT_GB_VOICE_ALLOWLIST


# ------------------------------------------------------------------- stage

def do_stage(root: Path, plan_only: bool, keep_non_cloning: bool) -> int:
    plans, notes = {}, []
    try:
        previous_manifest = json.loads((root / MANIFEST_NAME).read_text())
    except (OSError, ValueError):
        previous_manifest = {}
    for name, meta in STUDIOS.items():
        hub, models = discover(meta["port"])
        if hub is None or not hub.is_dir():
            notes.append(f"{meta['label']} not reachable on :{meta['port']} — skipped")
            continue
        stale = staleness(meta["port"], hub)
        if stale:
            notes.append(f"{meta['label']} {stale}")
        families, floors, _all_companions = catalog_index(models)
        cache_states = catalog_cache_states(models)
        jobs = []
        for src in sorted(hub.iterdir()):
            if not src.is_dir() or src.name.startswith("."):
                continue
            repo = dirname_to_repo(src.name)
            if repo is None:
                continue
            if cache_states.get(repo) not in (None, "cached"):
                notes.append(f"{meta['label']} skipped partial package {repo}")
                continue
            fam = families.get(repo)
            if fam is None:
                if "whisper" in repo.lower():
                    fam = STT_FAMILY
                else:
                    continue  # not in this studio's catalogue; don't ship junk
            jobs.append((src, repo, fam.replace("/", "-"), floors.get(repo)))
        plans[name] = {"hub": hub, "jobs": jobs}

    if not plans:
        for n in notes:
            print(f"  {n}")
        sys.exit("no studio caches found — start the studios first")

    total = 0
    for name, p in plans.items():
        sub = sum(dir_bytes(s) for s, _, _, _ in p["jobs"])
        total += sub
        print(f"{STUDIOS[name]['label']}: {len(p['jobs'])} packages, "
              f"{sub / 1e9:.1f} GB\n  from {p['hub']}")
        by_fam: dict[str, int] = {}
        for src, _, fam, _ in p["jobs"]:
            by_fam[fam] = by_fam.get(fam, 0) + dir_bytes(src)
        for fam, b in sorted(by_fam.items(), key=lambda kv: -kv[1]):
            print(f"    {fam[:44]:46} {b / 1e9:6.2f} GB")
        print()
    for n in notes:
        print(f"  note: {n}")
    voices = discover_fleet_voices()
    print(f"Fleet voices: {len(voices)} stable references")
    print("  existing SSD packages and voices not seen today are preserved")
    print(f"total: {total / 1e9:.1f} GB -> {root}")

    if plan_only:
        print("\n--plan only, nothing written.")
        return 0

    root.mkdir(parents=True, exist_ok=True)
    if not supports_symlinks(root):
        sys.exit(
            f"\nABORT: {root} cannot store symlinks (exFAT/FAT32?). Copying there "
            "would dereference the Hugging Face cache and roughly double its "
            "size. Reformat the drive as APFS."
        )
    manifest = {"schema_version": 2,
                "layout": "<studio>/<family>/models--org--name",
                "studios": {}, "voices": []}
    for name, p in plans.items():
        entries_by_repo = {
            entry["repo"]: entry
            for entry in (
                (previous_manifest.get("studios", {}).get(name) or {}).get("packages", [])
            )
            if isinstance(entry, dict) and entry.get("repo")
        }
        for i, (src, repo, fam, floor) in enumerate(p["jobs"], 1):
            dst_dir = root / name / fam
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / src.name
            action = copy_package_if_changed(src, dst)
            print(f"[{name} {i}/{len(p['jobs'])}] {action:7} {fam}/{src.name}")
            entries_by_repo[repo] = {
                "repo": repo, "dir": src.name, "family": fam,
                "floor_gb": floor, "bytes": dir_bytes(dst),
            }
        manifest["studios"][name] = {
            "packages": sorted(entries_by_repo.values(), key=lambda entry: entry["repo"])
        }
    for name in STUDIOS:
        if name not in manifest["studios"]:
            manifest["studios"][name] = (
                previous_manifest.get("studios", {}).get(name) or {"packages": []}
            )

    voice_entries = {
        entry["id"]: entry
        for entry in previous_manifest.get("voices", [])
        if isinstance(entry, dict) and entry.get("id")
    }
    for i, voice in enumerate(voices, 1):
        action, entry = stage_voice(root, voice)
        print(f"[voice {i}/{len(voices)}] {action:8} {entry['name']} ({entry['id']})")
        if action != "conflict":
            voice_entries[entry["id"]] = entry
        else:
            print("  protected: the SSD already has different audio under this stable ID")
    manifest["voices"] = sorted(voice_entries.values(), key=lambda entry: entry["id"])

    manifest_path = root / MANIFEST_NAME
    write_json_atomic(manifest_path, manifest)
    make_portable_readable(root)
    manifest_path.chmod(0o644)
    written = sum(e["bytes"] for s in manifest["studios"].values()
                  for e in s["packages"])
    print(f"\nDone: {written / 1e9:.1f} GB indexed, "
          f"{len(manifest['voices'])} voices.\nManifest: {root / MANIFEST_NAME}")
    print("\nOn each fleet Mac:\n    python3 tools/studio_models.py restore --plan")
    return 0


# ----------------------------------------------------------------- restore

def do_restore(root: Path, *, plan_only: bool, prune: bool, restore_all: bool,
               force: bool, include_unqualified: bool,
               keep_non_cloning: bool,
               pinokio_home: Path | None = None) -> int:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        sys.exit(f"no {MANIFEST_NAME} under {root} — is the SSD plugged in?")
    manifest = json.loads(manifest_path.read_text())

    ram = machine_memory_gb()
    print(f"this Mac: {ram:.1f} GB unified memory")
    print(f"source  : {root}\n")

    copied = replaced = intact = skipped = pruned = 0
    voices_copied = voices_intact = voice_conflicts = 0
    pruned_bytes = 0

    for name, meta in STUDIOS.items():
        staged = (manifest.get("studios", {}).get(name) or {}).get("packages", [])
        if not staged:
            continue
        if pinokio_home is None:
            hub, models = discover(meta["port"])
        else:
            hub, models = installed_hub(pinokio_home, f"{name}studio-mac"), []
        if hub is None:
            if staged:
                print(f"{meta['label']}: not installed or not running "
                      f"on :{meta['port']} — skipped\n")
            continue
        if pinokio_home is not None and not plan_only:
            hub.mkdir(parents=True, exist_ok=True)

        families, local_floors, _all_companions = catalog_index(models)
        companions = needed_companions(models)
        caps = {m["repo"]: (m.get("capabilities") or []) for m in models}
        ssd_floors = {e["repo"]: e.get("floor_gb") for e in staged}
        state_of = {m["repo"]: (m.get("cache") or {}).get("state")
                    for m in models}

        print(f"{meta['label']}  ({hub})")

        # ---- restore
        want, defer, unqualified = [], [], []
        for e in staged:
            floor = resolve_floor(e["repo"], local_floors, ssd_floors)
            floor = restore_floor(name, e["repo"], ram, floor)
            if not restore_allowed(name, e["repo"], ram, restore_all):
                defer.append((e, floor))
                continue
            if restore_all:
                want.append((e, floor))
            elif floor is None:
                # An unmeasured model used to be skipped, on the grounds that
                # nobody had established it runs here. That guard predates the
                # family allowlist: a model only reaches this point now because
                # its family was deliberately chosen, so membership already is
                # the decision.
                want.append((e, floor))
                # Only *models* are worth flagging as unmeasured. Whisper and
                # the codecs have no catalogue floor because they are not voice
                # models at all, and reporting "may not run here" for the
                # speech-to-text checker is noise that trains you to ignore the
                # warning that matters.
                if e["repo"] in caps:
                    unqualified.append((e, floor))
            elif floor <= ram:
                want.append((e, floor))
            else:
                defer.append((e, floor))

        for i, (e, floor) in enumerate(want, 1):
            src = root / name / e["family"] / e["dir"]
            if not src.is_dir():
                continue
            dst = hub / e["dir"]
            tag = f"[{i}/{len(want)}] {e['dir'][:52]}"
            if dst.exists():
                state = state_of.get(e["repo"])
                if state is None:
                    # Whisper and the codecs are not in the studio's model
                    # catalogue, so it reports no cache state for them. Treating
                    # "no state" as "untrustworthy" re-copied 1.6 GB of Whisper
                    # onto every machine that already had a perfect copy. Fall
                    # back to comparing what is on disk against what was staged.
                    staged_bytes = e.get("bytes") or 0
                    if staged_bytes and dir_bytes(dst) >= staged_bytes * 0.98:
                        state = "cached"
                if force:
                    action, note = "replace", "--force"
                elif state == "cached":
                    intact += 1
                    if plan_only:
                        print(f"  {tag} — already complete")
                    continue
                else:
                    # partial / phantom / unknown: the local copy cannot be
                    # trusted and the SSD copy is known good.
                    action, note = "replace", f"local is {state or 'unverified'}"
            else:
                action, note = "copy", ""

            print(f"  {tag} — {action}{' (' + note + ')' if note else ''}")
            if plan_only:
                copied += action == "copy"
                replaced += action == "replace"
                continue
            if dst.exists():
                shutil.rmtree(dst)
                replaced += 1
            else:
                copied += 1
            shutil.copytree(src, dst, symlinks=True)

        skipped += len(defer)
        if defer:
            print(f"  skipped {len(defer)} needing more memory than this Mac has"
                  f" ({', '.join(sorted({str(f) + ' GB' for _, f in defer}))})")
        if unqualified:
            gb = sum(e["bytes"] for e, _ in unqualified) / 1e9
            print(f"  installing {len(unqualified)} with no measured memory floor "
                  f"({gb:.1f} GB) — they may not run here; report it if they fail "
                  f"and the floor gets raised:")
            for e, _ in unqualified:
                print(f"      {e['repo']}")

        # ---- prune
        if prune:
            victims = []
            for d in sorted(hub.iterdir()):
                if not d.is_dir() or not d.name.startswith("models--"):
                    continue
                repo = dirname_to_repo(d.name)
                if repo is None or repo in companions:
                    continue  # never prune a codec/tokenizer
                floor = resolve_floor(repo, local_floors, ssd_floors)
                if floor is not None and floor > ram:
                    victims.append((d, repo, floor, dir_bytes(d)))
            for d, repo, floor, b in victims:
                if floor:
                    why = f"needs {floor:>2.0f} GB"
                else:
                    why = "unqualified"
                print(f"  prune {repo[:48]:50} {why:>12}  {b / 1e9:5.2f} GB")
                pruned += 1
                pruned_bytes += b
                if not plan_only:
                    shutil.rmtree(d)
            if not victims:
                print("  nothing to prune")
        print()

    staged_voices = manifest.get("voices") or []
    if staged_voices:
        if pinokio_home is not None:
            voice_studio = installed_studio(pinokio_home, "voicestudio-mac")
        else:
            voice_hub, _models = discover(STUDIOS["voice"]["port"])
            voice_studio = voice_hub.parents[2] if voice_hub is not None else None
        if voice_studio is None:
            print("Fleet voices: Voice Studio is not installed — skipped\n")
        else:
            voices_dir = voice_studio / "app/voices"
            print(f"Fleet voices  ({voices_dir})")
            for entry in staged_voices:
                voice_id, _digest = validate_voice_entry(entry)
                source = root / "voices" / voice_id
                if not source.is_dir():
                    continue
                if plan_only:
                    destination = voices_dir / str(entry["id"])
                    action = "inspect" if destination.exists() else "copy"
                else:
                    action = restore_voice(source, voices_dir, entry)
                print(f"  {action:8} {entry.get('name') or entry['id']}")
                if action == "copy":
                    voices_copied += 1
                elif action == "intact":
                    voices_intact += 1
                elif action == "conflict":
                    voice_conflicts += 1
                    print("    protected: local audio under this stable ID was not overwritten")
            print()

    verb = "would " if plan_only else ""
    print(f"{verb}copy {copied} new, {verb}replace {replaced}, "
          f"{intact} already complete, {skipped} above this Mac's memory.")
    if staged_voices:
        print(f"Fleet voices: {voices_copied} copied, {voices_intact} already complete, "
              f"{voice_conflicts} protected conflicts.")
    if prune:
        print(f"{verb}prune {pruned} unusable models, freeing {pruned_bytes / 1e9:.1f} GB.")
    if plan_only:
        print("\n--plan only, nothing written or deleted.")
    else:
        print("\nDone. Restart the studios (or click Update) so they rescan.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("stage", help="copy this Mac's caches onto the SSD")
    s.add_argument("--root", type=Path,
                   help="SSD model folder (default: auto-detect ugreen-terranash or a manifest)")
    s.add_argument("--plan", action="store_true", help="show the plan, write nothing")
    s.add_argument("--keep-non-cloning", action="store_true",
                   help="accepted for compatibility; all cached catalog models are staged")

    r = sub.add_parser("restore", help="install onto this Mac from the SSD")
    r.add_argument("--root", type=Path,
                   help="SSD model folder (default: auto-detect ugreen-terranash or a manifest)")
    r.add_argument("--plan", action="store_true",
                   help="show the plan, write and delete nothing")
    r.add_argument("--prune", action="store_true",
                   help="also delete models this Mac has too little memory to run")
    r.add_argument("--all", action="store_true",
                   help="restore everything, ignoring this Mac's memory tier")
    r.add_argument("--force", action="store_true",
                   help="replace local copies even when already complete")
    r.add_argument("--keep-non-cloning", action="store_true",
                   help="accepted for compatibility; only explicit RAM pruning is applied")
    r.add_argument("--include-unqualified", action="store_true",
                   help="accepted for compatibility; unmeasured models in a "
                        "stocked family are installed by default now")
    r.add_argument("--pinokio-home", type=Path,
                   help="copy directly into installed Studio caches without starting them")

    args = ap.parse_args()
    args.root = args.root or find_default_root()
    if args.command == "stage":
        return do_stage(args.root, args.plan, args.keep_non_cloning)
    return do_restore(args.root, plan_only=args.plan, prune=args.prune,
                      restore_all=args.all, force=args.force,
                      include_unqualified=args.include_unqualified,
                      keep_non_cloning=args.keep_non_cloning,
                      pinokio_home=args.pinokio_home)


if __name__ == "__main__":
    raise SystemExit(main())
