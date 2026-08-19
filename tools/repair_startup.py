#!/usr/bin/env python3
"""Repair Studio startup ownership without starting, stopping, or deleting apps."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


APPS = ("imagestudio-mac", "voicestudio-mac", "studiohub-mac")


def update_environment(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(values)
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if remaining:
        if output and output[-1]:
            output.append("")
        output.append("# TerraNash startup repair: each Studio owns its startup independently.")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n")
    temporary.replace(path)


def resolve_pinokio_home() -> Path | None:
    try:
        configured = json.loads((Path.home() / ".pinokio/config.json").read_text()).get("home")
    except (OSError, ValueError):
        configured = None
    if configured:
        return Path(str(configured)).expanduser().resolve()
    try:
        with urllib.request.urlopen("http://127.0.0.1:42000/pinokio/home", timeout=2) as reply:
            live = json.loads(reply.read()).get("path")
    except (OSError, ValueError, urllib.error.URLError):
        live = None
    if live:
        return Path(str(live)).expanduser().resolve()
    fallback = os.environ.get("PINOKIO_HOME", "").strip()
    return Path(fallback).expanduser().resolve() if fallback else None


def repair_startup(home: Path, *, dry_run: bool) -> int:
    found = 0
    for app_name in APPS:
        candidates = [
            path for path in (home / "api" / app_name, home / "api" / f"{app_name}.git")
            if path.is_dir()
        ]
        if not candidates:
            print(f"  {app_name}: not installed — skipped")
            continue
        service_owners = [
            target for target in candidates if (target / "service/.installed").is_file()
        ]
        active = service_owners[0] if len(service_owners) == 1 else candidates[0]
        ambiguous_services = len(service_owners) > 1
        if ambiguous_services:
            print(f"  {app_name}: multiple service markers — disabling Pinokio for every checkout")
        for target in candidates:
            service_owned = target in service_owners
            pinokio_enabled = not service_owners and target == active
            owner = "launchd service" if service_owned else "Pinokio autolaunch" if pinokio_enabled else "disabled duplicate"
            print(f"  {target.name}: {owner}; no Studio dependencies")
            if not dry_run:
                update_environment(target / "ENVIRONMENT", {
                    "PINOKIO_SCRIPT_AUTOLAUNCH": "start.js",
                    "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED": "true" if pinokio_enabled else "false",
                    "PINOKIO_SCRIPT_REQUIRES": "",
                })
            found += 1
    if not found:
        raise RuntimeError(f"No Studio checkouts found under {home / 'api'}")
    print("\nStartup settings repaired. Restart Pinokio when convenient; no app was started or stopped.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pinokio-home", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    home = args.pinokio_home or resolve_pinokio_home()
    if home is None:
        parser.error("PINOKIO_HOME could not be resolved; open Pinokio and retry")
    return repair_startup(home, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
