#!/usr/bin/env python3
"""Survey the fleet's local model caches and repair what's broken or missing.

A job routed to a machine that can't actually run the model is a job that
fails slowly. Two things cause that: caches that have quietly broken
(interrupted downloads leave partials; a cache directory can outlive its own
weight files and still report ``cached``), and machines that never received a
model for some capability at all -- a Mac with no clone-capable voice model
cannot serve an Aiden job no matter how healthy it looks.

This walks every machine and reports both, and can fix both.

    python3 tools/fleet_repair.py                    # survey only (default)
    python3 tools/fleet_repair.py --apply            # repair broken caches
    python3 tools/fleet_repair.py --apply --fill-gaps  # also cover capabilities
    python3 tools/fleet_repair.py --studio image     # one studio
    python3 tools/fleet_repair.py --machine <id-fragment>   # one machine

Nothing is ever deleted. Repair only starts downloads, so a wrong call costs
bandwidth, not a cache.

Two deliberate choices about scope:

* Eligibility comes from the studio, not from this script. Voice Studio already
  publishes ``memory_eligible`` per model for the machine asking; recomputing
  RAM-versus-floor here would mean two sources of truth that drift apart.
* Gap filling covers *capabilities*, not the catalog. Installing every model a
  machine qualifies for would mean hundreds of downloads and hundreds of GB;
  what actually matters is that each machine has at least one working model for
  each capability it may be asked to serve.

Both studios expose the same three endpoints -- ``/api/system``,
``/api/catalog`` and ``/api/downloads`` -- so one code path serves both.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Tokens live OUTSIDE the repo so they can never be committed. chmod 600.
TOKENS_FILE = Path.home() / ".voicestudio-fleet-tokens.json"

STUDIOS = {
    "voice": 47870,
    "image": 47868,
}

# Capabilities every machine should be able to serve, and the order in which
# candidates are preferred when one is missing. A machine that covers these can
# take work; one that doesn't is silently dead weight in the routing pool.
ESSENTIAL_CAPABILITIES = {
    "voice": ("voice-cloning", "tts"),
    "image": ("txt2img",),
}

# The machine table is private infrastructure detail and this repository is
# public, so it lives in an untracked local file rather than here. RAM is not
# recorded in it either: that is read live from each machine's /api/system,
# because a hardcoded RAM table goes stale after a hardware swap and then
# silently plans the wrong model set for the rest of the fleet's life. The same
# argument applies to the addresses, so there is no built-in fallback list.
MACHINES_FILE = Path(os.environ.get("FLEET_MACHINES_FILE")
                     or Path(__file__).resolve().parents[1] / "fleet_machines.json")

MACHINES_FORMAT = ('  {"machines": [{"id": "<machine-id>", "ip": "<address>", '
                   '"token_key": "<fleet-token-key>"}]}')


def load_machines() -> list[dict]:
    """Fleet machine records, read from the untracked local config."""
    if not MACHINES_FILE.exists():
        raise SystemExit(
            f"missing fleet machine table: {MACHINES_FILE}\n"
            "Create it (chmod 600), or point FLEET_MACHINES_FILE at an existing "
            "copy. Expected format:\n" + MACHINES_FORMAT)
    try:
        data = json.loads(MACHINES_FILE.read_text())
    except ValueError as exc:
        raise SystemExit(f"{MACHINES_FILE}: not valid JSON -- {exc}") from exc
    machines = data.get("machines") if isinstance(data, dict) else data
    if not isinstance(machines, list) or not machines:
        raise SystemExit(f"{MACHINES_FILE}: no machines listed. Expected format:\n"
                         + MACHINES_FORMAT)
    bad = [m for m in machines
           if not isinstance(m, dict) or not m.get("id") or not m.get("ip")]
    if bad:
        raise SystemExit(f"{MACHINES_FILE}: every entry needs 'id' and 'ip' -- "
                         f"first bad entry: {bad[0]!r}")
    return machines

# A cache dir can lose its weight files and still report ``cached`` -- seen on
# a 16 GB fleet Mac whose Kokoro cache held 10% of the model while the studio
# reported it available and runtime-ready, so every job routed there failed at
# load time.
#
# Detecting that by comparing bytes against the catalog's ``size_gb`` does not
# work: that figure is a hand-maintained approximation and is routinely off.
# chatterbox-turbo-4bit measures 0.69 of its claimed size on all six machines
# that hold it -- the catalog is stale, the caches are fine. An absolute
# threshold called all six broken.
#
# The fleet is its own reference. The same repo on many machines should measure
# the same; a machine well below its peers has really lost data, whatever the
# catalog claims. Only fall back to an absolute fraction when too few machines
# hold the repo to form a consensus.
# Healthy copies of the same repo measure the same to within rounding: across
# the fleet, chatterbox-turbo reads 0.69 on all six machines holding it,
# VibeVoice 1.05 on seven, MOSS 0.98 on five. Observed spread among healthy
# peers is effectively zero, so the tolerance below is already generous --
# 0.70 was not, and passed a MOSS cache sitting 20% under its peers.
PEER_MIN_SAMPLES = 3        # machines needed before peer consensus is trusted
PEER_BROKEN_RATIO = 0.90    # below this share of the peer median = lost data
ABSOLUTE_FRACTION = 0.50    # lone-copy fallback, deliberately forgiving


def token_for(machine_id: str, tokens: dict, token_keys: dict) -> str:
    """The fleet token covering this machine, named by its own config entry."""
    return (tokens.get(token_keys.get(machine_id) or "") or {}).get("token", "")


def api(ip: str, port: int, path: str, token: str, *, body: dict | None = None,
        timeout: float = 20.0) -> dict | None:
    """One fleet API call. Returns None when the machine or studio is unreachable."""
    url = f"http://{ip}:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("X-Hub-Token", token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None


def is_cloud(model: dict) -> bool:
    """Cloud-provider entries have no local weights to manage.

    They are marked ``provider: cloud`` in Image Studio and carry a zero size
    in both studios. Their repo strings still look like ``owner/name``, so
    testing the repo shape is not enough -- an earlier version of this script
    queued 38 downloads for models that live on someone else's GPU.
    """
    return model.get("provider") == "cloud" or not (model.get("size_gb") or 0)


def eligible(model: dict, ram_gb: float) -> bool:
    """Should this model live on this machine's disk?

    Deliberately the *static* floor, not Voice Studio's ``memory_eligible``.
    That field is ``total >= floor and available >= required`` -- its second
    term is live free memory, so it answers "can I load this right now", which
    flips depending on what else happens to be open. Provisioning is a question
    about the hardware, not about this instant: a machine with Chrome running
    must not be told to skip a model it can run perfectly well once Chrome
    quits. Runtime admission stays the studio's call; disk contents are this
    script's.

    A null floor means the model has not been memory-qualified on real hardware
    yet. Those are never provisioned automatically -- pushing an unmeasured
    model onto an 8 GB Mac is how a machine ends up swapping through every job.
    """
    floor = model.get("min_unified_memory_gb")
    return floor is not None and floor <= ram_gb


def peer_medians(surveyed: list[dict]) -> dict[str, float]:
    """Median completeness ratio per repo across every machine holding it.

    Built only from caches the studios call ``cached``, so a half-finished
    download on one machine cannot drag the reference down for the rest.
    """
    samples: dict[str, list[float]] = {}
    for machine in surveyed:
        for data in machine.get("studios", {}).values():
            for m in data.get("models", []):
                if m["cache_state"] != "cached" or not m["expected"]:
                    continue
                samples.setdefault(m["repo"], []).append(m["complete"] / m["expected"])
    medians = {}
    for repo, values in samples.items():
        if len(values) < PEER_MIN_SAMPLES:
            continue
        values.sort()
        mid = len(values) // 2
        medians[repo] = (values[mid] if len(values) % 2
                         else (values[mid - 1] + values[mid]) / 2)
    return medians


def classify(model: dict, ram_gb: float, peer: float | None) -> tuple[str, str]:
    """Return (state, reason) for one model on one machine.

    States: ok, partial, phantom, absent, stranded, unqualified.
    ``absent`` is not a defect -- most of the catalog is legitimately absent
    from most machines. Only the capability check turns absence into work.
    """
    complete = model["complete"]
    incomplete = model["incomplete"]
    expected = model["expected"]
    on_disk = complete + incomplete
    ratio = (complete / expected) if expected else 0.0

    if model["cache_state"] != "cached":
        intact = False
    elif peer is not None:
        intact = ratio >= peer * PEER_BROKEN_RATIO
    else:
        intact = ratio >= ABSOLUTE_FRACTION

    floor = model.get("min_unified_memory_gb")
    if floor is None:
        # Not memory-qualified yet, so there is no floor to compare against.
        # Say that, rather than printing a comparison against None.
        if on_disk:
            return "unqualified", f"{on_disk / 1e9:.2f} GB cached, no measured floor"
        return "ok", "absent (no measured floor)"

    if floor > ram_gb:
        if on_disk:
            # Disk spent on a model this machine can never run. Worth a human
            # deciding to reclaim, but never something to resume.
            return "stranded", f"{on_disk / 1e9:.2f} GB cached, needs {floor} GB > {ram_gb:.0f} GB RAM"
        return "ok", f"correctly absent (needs {floor} GB)"

    if intact:
        return "ok", "cached"
    if model["cache_state"] == "cached":
        if peer is not None:
            return "phantom", (f"claims cached at {ratio:.0%} of catalog size; "
                               f"the fleet holds {peer:.0%}")
        return "phantom", (f"claims cached, holds {complete / 1e9:.2f} of "
                           f"{expected / 1e9:.2f} GB (no peers to compare)")
    if on_disk:
        # size_gb is a rounded catalog approximation, so a repo can hold more
        # bytes than it claims and still be missing files. Printing "1.89 of
        # 1.80 GB" reads like a bug in the check rather than a real partial.
        if on_disk < expected:
            return "partial", f"{on_disk / 1e9:.2f} of {expected / 1e9:.2f} GB"
        return "partial", f"{on_disk / 1e9:.2f} GB on disk, files still missing"
    return "absent", "not installed"


def survey_machine(machine_id: str, ip: str, token: str, studios: list[str]) -> dict:
    """Read one machine's RAM and per-studio catalog state."""
    result: dict = {"id": machine_id, "ip": ip, "online": False, "studios": {}}

    # Probe RAM via whichever studio answers first; they report the same host.
    for name in studios:
        system = api(ip, STUDIOS[name], "/api/system", token, timeout=10)
        if system:
            result["online"] = True
            result["ram_gb"] = system.get("unified_memory_gb") or 0
            result["chip"] = system.get("chip") or "?"
            break
    if not result["online"]:
        return result

    for name in studios:
        port = STUDIOS[name]
        catalog = api(ip, port, "/api/catalog", token)
        if catalog is None:
            result["studios"][name] = {"reachable": False, "models": []}
            continue

        running = api(ip, port, "/api/downloads", token) or {}
        in_flight = {
            j.get("repo") for j in (running.get("jobs") or [])
            if j.get("state") in ("queued", "running")
        }

        models = []
        for m in catalog.get("models", []):
            if is_cloud(m):
                continue
            cache = m.get("cache") or {}
            models.append({
                "repo": m["repo"],
                "label": m.get("label") or m["repo"],
                "size_gb": m.get("size_gb") or 0,
                "min_unified_memory_gb": m.get("min_unified_memory_gb"),
                "capabilities": m.get("capabilities") or [],
                "eligible": eligible(m, result["ram_gb"]),
                "recommended": "recommended" in (m.get("label") or "").lower(),
                "cache_state": cache.get("state"),
                "complete": cache.get("bytes_complete") or 0,
                "incomplete": cache.get("bytes_incomplete") or 0,
                "expected": (m.get("size_gb") or 0) * 1_000_000_000,
                "downloading": m["repo"] in in_flight,
            })
        result["studios"][name] = {"reachable": True, "models": models}
    return result


def assign_states(surveyed: list[dict]) -> None:
    """Second pass: classify every model once the whole fleet is visible.

    Classification cannot happen during collection because deciding whether one
    machine's cache is broken depends on what the other machines hold.
    """
    peers = peer_medians(surveyed)
    for machine in surveyed:
        if not machine["online"]:
            continue
        for data in machine.get("studios", {}).values():
            for m in data.get("models", []):
                if m["downloading"]:
                    m["state"], m["reason"] = "downloading", "already downloading"
                    continue
                m["state"], m["reason"] = classify(
                    m, machine["ram_gb"], peers.get(m["repo"]))


# Broken caches for models the machine can run. Always worth fixing.
DEFECTS = ("partial", "phantom")


def capability_gaps(studio: str, models: list[dict]) -> list[tuple[str, dict]]:
    """Essential capabilities with no working model, and the best candidate.

    A capability counts as covered if any eligible model providing it is
    cached or already downloading -- a machine mid-download does not need a
    second copy of the same capability queued behind it.
    """
    gaps = []
    for capability in ESSENTIAL_CAPABILITIES.get(studio, ()):
        providers = [m for m in models
                     if capability in m["capabilities"] and m["eligible"]]
        if any(m["state"] in ("ok", "downloading") for m in providers):
            continue
        candidates = [m for m in providers if m["state"] in ("absent", *DEFECTS)]
        if not candidates:
            continue
        # Prefer what the catalog itself recommends, then the smallest, so the
        # machine becomes useful as soon as possible.
        candidates.sort(key=lambda m: (not m["recommended"], m["size_gb"]))
        gaps.append((capability, candidates[0]))
    return gaps


def build_plan(machine: dict, *, fill_gaps: bool, cap: int) -> list[dict]:
    """Downloads to start on this machine, smallest first, capped per run."""
    todo: dict[str, dict] = {}
    for studio, data in machine.get("studios", {}).items():
        models = data.get("models", [])
        for m in models:
            if m["state"] in DEFECTS:
                todo[m["repo"]] = {**m, "studio": studio, "why": f"repair {m['state']}"}
        if fill_gaps:
            for capability, candidate in capability_gaps(studio, models):
                todo.setdefault(candidate["repo"],
                                {**candidate, "studio": studio,
                                 "why": f"no {capability} model"})
    # Smallest first: the machine gets back to useful sooner, and a large
    # wedged download can't block several small ones behind it.
    return sorted(todo.values(), key=lambda m: m["size_gb"])[:cap]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="start the repair downloads (default is survey only)")
    parser.add_argument("--fill-gaps", action="store_true",
                        help="also install a model for any uncovered essential capability")
    parser.add_argument("--studio", choices=sorted(STUDIOS), action="append",
                        help="limit to one studio (repeatable; default both)")
    parser.add_argument("--machine", action="append",
                        help="limit to machines whose id contains this (repeatable)")
    parser.add_argument("--max-per-machine", type=int, default=2,
                        help="downloads to start per machine per run (default 2)")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="emit the full survey as JSON")
    args = parser.parse_args()

    if not TOKENS_FILE.exists():
        print(f"missing fleet tokens at {TOKENS_FILE}", file=sys.stderr)
        return 2
    tokens = json.loads(TOKENS_FILE.read_text())

    machines = load_machines()
    hosts = {m["id"]: m["ip"] for m in machines}
    token_keys = {m["id"]: m.get("token_key") or "" for m in machines}

    studios = args.studio or sorted(STUDIOS)
    selected = {mid for mid in hosts
                if not args.machine or any(f in mid for f in args.machine)}
    if not selected:
        print("no machines matched", file=sys.stderr)
        return 2

    # Always survey the whole fleet, even when --machine narrows the report.
    # Peer comparison is only meaningful against the full population: surveying
    # just the machines named on the command line means the reference is drawn
    # from exactly the machines suspected of being broken, and a run targeting
    # two damaged caches would take their damage as the fleet norm and call
    # them healthy.
    scope = (f"{len(selected)} of {len(hosts)} machines"
             if len(selected) < len(hosts) else f"{len(hosts)} machines")
    # Progress goes to stderr under --json so stdout stays pipeable into jq.
    print(f"surveying {scope} across {', '.join(studios)} ...\n",
          file=sys.stderr if args.as_json else sys.stdout)
    with ThreadPoolExecutor(max_workers=8) as pool:
        surveyed = list(pool.map(
            lambda kv: survey_machine(kv[0], kv[1],
                                      token_for(kv[0], tokens, token_keys), studios),
            sorted(hosts.items()),
        ))
    assign_states(surveyed)
    surveyed = [m for m in surveyed if m["id"] in selected]

    if args.as_json:
        print(json.dumps(surveyed, indent=2))
        return 0

    offline = [m for m in surveyed if not m["online"]]
    online = [m for m in surveyed if m["online"]]

    totals: dict[str, int] = {}
    for machine in online:
        rows = [(s, m) for s, data in machine["studios"].items()
                for m in data.get("models", [])]
        for _, m in rows:
            totals[m["state"]] = totals.get(m["state"], 0) + 1

        defects = [(s, m) for s, m in rows if m["state"] in DEFECTS]
        stranded = [(s, m) for s, m in rows if m["state"] == "stranded"]
        gaps = [(s, cap, cand)
                for s, data in machine["studios"].items()
                for cap, cand in capability_gaps(s, data.get("models", []))]

        headline = []
        if defects:
            headline.append(f"{len(defects)} broken")
        if gaps:
            headline.append(f"{len(gaps)} uncovered")
        flag = f"  <-- {', '.join(headline)}" if headline else ""
        print(f"{machine['id']}  {machine.get('chip', '?'):10} "
              f"{machine.get('ram_gb', 0):>3.0f} GB{flag}")

        for studio, m in defects:
            print(f"    {m['state']:9} {studio:6} {m['label'][:40]:42} {m['reason']}")
        for studio, capability, candidate in gaps:
            print(f"    {'uncovered':9} {studio:6} no {capability} model "
                  f"-> {candidate['label'][:34]} ({candidate['size_gb']:.1f} GB)")
        for studio, m in stranded:
            print(f"    {'stranded':9} {studio:6} {m['label'][:40]:42} {m['reason']}")
        for studio, m in [(s, m) for s, m in rows if m["state"] == "unqualified"]:
            print(f"    {'unqualif.':9} {studio:6} {m['label'][:40]:42} {m['reason']}")
        for studio, data in machine["studios"].items():
            if not data.get("reachable"):
                print(f"    {'offline':9} {studio:6} studio not responding")

    if offline:
        print(f"\noffline ({len(offline)}): {', '.join(m['id'] for m in offline)}")
    print("\nfleet totals: " + "  ".join(f"{k}={v}" for k, v in sorted(totals.items())))

    plans = {m["id"]: build_plan(m, fill_gaps=args.fill_gaps,
                                 cap=args.max_per_machine) for m in online}
    to_start = sum(len(p) for p in plans.values())
    queued_gb = sum(x["size_gb"] for p in plans.values() for x in p)

    if not to_start:
        extra = "" if args.fill_gaps else "  (add --fill-gaps to cover capabilities too)"
        print(f"\nnothing to repair.{extra}")
        return 0

    if not args.apply:
        print(f"\nwould start {to_start} downloads ({queued_gb:.1f} GB). "
              f"re-run with --apply to do it.")
        for mid, plan in sorted(plans.items()):
            for item in plan:
                print(f"    {mid}  {item['studio']:6} {item['label'][:40]:42} "
                      f"{item['size_gb']:>5.1f} GB  {item['why']}")
        return 0

    print(f"\nstarting {to_start} downloads ({queued_gb:.1f} GB) ...")
    started = failed = 0
    for machine in online:
        token = token_for(machine["id"], tokens, token_keys)
        for item in plans[machine["id"]]:
            ok = api(machine["ip"], STUDIOS[item["studio"]], "/api/downloads",
                     token, body={"repo": item["repo"]})
            status = "started" if ok else "FAILED "
            started, failed = (started + 1, failed) if ok else (started, failed + 1)
            print(f"    {status}  {machine['id']}  {item['studio']:6} {item['label'][:40]}")

    print(f"\nstarted {started}, failed {failed}. "
          f"re-run to check progress and queue the next batch.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
