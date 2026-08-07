#!/usr/bin/env python3
"""Stage every studio's model cache onto an SSD, then restore and prune it on
each fleet Mac in one pass.

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

3. **A stale local catalogue cannot cause a wrong delete.** Memory floors are
   resolved as the highest value any source knows, because a machine running an
   older studio reports a floor of `None` for a model that has since been
   measured. Taking that at face value would have deleted a perfectly good Fish
   Audio cache off a 16 GB worker.

Pruning only ever removes a model whose floor is *known* and higher than this
Mac's memory -- something this machine physically cannot run. Companions
(codecs, tokenizers) are never pruned; they are small and a model without its
codec is useless.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

STUDIOS = {
    "voice": {"port": 47870, "label": "Voice Studio"},
    "image": {"port": 47868, "label": "Image Studio"},
}

DEFAULT_ROOT = Path("/Volumes/UGREEN-1TB/studio-models")
MANIFEST_NAME = "MANIFEST.json"
STT_FAMILY = "Whisper (speech-to-text)"


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


def resolve_floor(repo: str, *sources: dict) -> float | None:
    """Highest floor any source knows.

    A studio that predates a model's qualification reports None. Treating that
    as "no requirement" is how a 16 GB-only model looks prunable on an 8 GB Mac
    -- or worse, looks prunable on the 16 GB Mac that can actually run it.
    """
    known = [s[repo] for s in sources if s.get(repo) is not None]
    return max(known) if known else None


# ------------------------------------------------------------------- stage

def do_stage(root: Path, plan_only: bool) -> int:
    plans, notes = {}, []
    for name, meta in STUDIOS.items():
        hub, models = discover(meta["port"])
        if hub is None or not hub.is_dir():
            notes.append(f"{meta['label']} not reachable on :{meta['port']} — skipped")
            continue
        stale = staleness(meta["port"], hub)
        if stale:
            notes.append(f"{meta['label']} {stale}")
        families, floors, _ = catalog_index(models)
        jobs = []
        for src in sorted(hub.iterdir()):
            if not src.is_dir() or src.name.startswith("."):
                continue
            repo = dirname_to_repo(src.name)
            if repo is None:
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

    manifest = {"schema_version": 1,
                "layout": "<studio>/<family>/models--org--name",
                "studios": {}}
    for name, p in plans.items():
        entries = []
        for i, (src, repo, fam, floor) in enumerate(p["jobs"], 1):
            dst_dir = root / name / fam
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / src.name
            if dst.exists():
                shutil.rmtree(dst)
            print(f"[{name} {i}/{len(p['jobs'])}] {fam}/{src.name}")
            shutil.copytree(src, dst, symlinks=True)   # symlinks=True is the point
            entries.append({"repo": repo, "dir": src.name, "family": fam,
                            "floor_gb": floor, "bytes": dir_bytes(dst)})
        manifest["studios"][name] = {"packages": entries}

    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))
    written = sum(e["bytes"] for s in manifest["studios"].values()
                  for e in s["packages"])
    print(f"\nDone: {written / 1e9:.1f} GB written.\nManifest: {root / MANIFEST_NAME}")
    print("\nOn each fleet Mac:\n    python3 tools/studio_models.py restore --plan")
    return 0


# ----------------------------------------------------------------- restore

def do_restore(root: Path, *, plan_only: bool, prune: bool, restore_all: bool,
               force: bool, include_unqualified: bool) -> int:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        sys.exit(f"no {MANIFEST_NAME} under {root} — is the SSD plugged in?")
    manifest = json.loads(manifest_path.read_text())

    ram = machine_memory_gb()
    print(f"this Mac: {ram:.1f} GB unified memory")
    print(f"source  : {root}\n")

    copied = replaced = intact = skipped = pruned = 0
    pruned_bytes = 0

    for name, meta in STUDIOS.items():
        staged = (manifest.get("studios", {}).get(name) or {}).get("packages", [])
        hub, models = discover(meta["port"])
        if hub is None:
            if staged:
                print(f"{meta['label']}: not installed or not running "
                      f"on :{meta['port']} — skipped\n")
            continue

        families, local_floors, companions = catalog_index(models)
        ssd_floors = {e["repo"]: e.get("floor_gb") for e in staged}
        state_of = {m["repo"]: (m.get("cache") or {}).get("state")
                    for m in models}

        print(f"{meta['label']}  ({hub})")

        # ---- restore
        want, defer, unqualified = [], [], []
        for e in staged:
            floor = resolve_floor(e["repo"], local_floors, ssd_floors)
            if restore_all:
                want.append((e, floor))
            elif floor is None:
                # No measured floor means nobody has established this model runs
                # here. Installing it anyway is how a small Mac ends up holding
                # models it swaps through -- and on the image side an unmeasured
                # model is 6 GB, not 600 MB. Opt in per run instead.
                (want if include_unqualified else unqualified).append((e, floor))
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
            print(f"  skipped {len(unqualified)} with no measured memory floor "
                  f"({gb:.1f} GB) — add --include-unqualified to install them:")
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
                print(f"  prune {repo[:48]:50} needs {floor:>2.0f} GB  "
                      f"{b / 1e9:5.2f} GB")
                pruned += 1
                pruned_bytes += b
                if not plan_only:
                    shutil.rmtree(d)
            if not victims:
                print("  nothing to prune")
        print()

    verb = "would " if plan_only else ""
    print(f"{verb}copy {copied} new, {verb}replace {replaced}, "
          f"{intact} already complete, {skipped} above this Mac's memory.")
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
    s.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    s.add_argument("--plan", action="store_true", help="show the plan, write nothing")

    r = sub.add_parser("restore", help="install onto this Mac from the SSD")
    r.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    r.add_argument("--plan", action="store_true",
                   help="show the plan, write and delete nothing")
    r.add_argument("--prune", action="store_true",
                   help="also delete models this Mac has too little memory to run")
    r.add_argument("--all", action="store_true",
                   help="restore everything, ignoring this Mac's memory tier")
    r.add_argument("--force", action="store_true",
                   help="replace local copies even when already complete")
    r.add_argument("--include-unqualified", action="store_true",
                   help="also install models with no measured memory floor "
                        "(use when trialling a model on a tier, e.g. Z-Image on 8 GB)")

    args = ap.parse_args()
    if args.command == "stage":
        return do_stage(args.root, args.plan)
    return do_restore(args.root, plan_only=args.plan, prune=args.prune,
                      restore_all=args.all, force=args.force,
                      include_unqualified=args.include_unqualified)


if __name__ == "__main__":
    raise SystemExit(main())
