"""Small, privacy-safe live activity view built from existing Studio polls.

This module intentionally has no HTTP client or background task.  Studios own
their job state; the monitor supplies already-authenticated, validated snapshots
and this module stores only bounded scalar transitions in the Hub ledger.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict

from . import control_plane, ledger

SCHEMA = "kh-studio.activity.v1"
VALID_STATES = frozenset({"queued", "running", "done", "error", "cancelled"})
ACTIVE_STATES = frozenset({"queued", "running"})
TERMINAL_STATES = frozenset({"done", "error", "cancelled"})
RETENTION_S = 30 * 86400
JUST_FINISHED_S = 15 * 60
LONG_IDLE_S = 2 * 3600
ACTIVE_FRESH_S = 30
REPORTER_CLOCK_SKEW_S = 5 * 60
_MAX_ID = 160
_MAX_MODEL = 500
_MAX_ERROR_CODE = 80
VALID_ORIGINS = frozenset({"hub", "local_ui", "api", "unknown"})
VALID_OPERATIONS = frozenset({"image", "speech", "transcription"})
_MAX_ORIGIN_DEVICE = 160


def _finite(value, *, minimum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        return None
    return value


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= limit else None


def _job(value: object, *, studio: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    job_id = _text(value.get("id"), _MAX_ID)
    state = value.get("state")
    model = _text(value.get("model"), _MAX_MODEL)
    source = value.get("source")
    if not job_id or state not in VALID_STATES or not model or source not in {"direct", "job"}:
        return None
    origin = value.get("origin", "unknown")
    if origin not in VALID_ORIGINS:
        return None
    origin_device = None
    if "origin_device" in value:
        origin_device = _text(value["origin_device"], _MAX_ORIGIN_DEVICE)
        if origin_device is None:
            return None
    progress = value.get("progress")
    if progress is not None:
        progress = _finite(progress, minimum=0)
        if progress is None or progress > 1:
            return None
    timestamps = {}
    for key in ("created_at", "started_at", "updated_at", "finished_at", "runtime_s"):
        number = value.get(key)
        if number is None:
            continue
        parsed = _finite(number, minimum=0)
        if parsed is None:
            return None
        timestamps[key] = parsed
    if state in TERMINAL_STATES and "finished_at" not in timestamps:
        return None
    created = timestamps.get("created_at")
    started = timestamps.get("started_at")
    updated = timestamps.get("updated_at")
    finished = timestamps.get("finished_at")
    runtime = timestamps.get("runtime_s")
    if any(created is not None and later is not None and created > later
           for later in (started, updated, finished)) or any(
               started is not None and later is not None and started > later
               for later in (updated, finished)
           ) or (updated is not None and finished is not None and updated > finished):
        return None
    if runtime is not None and started is not None and finished is not None:
        # Runtime is worker evidence, but cannot exceed the reported wall span.
        if runtime > finished - started + 1:
            return None
    error_code = _text(value.get("error_code"), _MAX_ERROR_CODE)
    operation = value.get("operation", "image" if studio == "image" else "speech")
    if operation not in VALID_OPERATIONS or (
        studio == "image" and operation != "image"
    ) or (studio == "voice" and operation == "image"):
        return None
    # Reporters may publish a safe generic error message; the Hub never
    # persists arbitrary error text from the worker.
    return {
        "id": job_id, "state": state, "model": model, "operation": operation,
        "source": source, "origin": origin,
        "progress": progress, "error_code": error_code, **timestamps,
        **({"origin_device": origin_device} if origin_device else {}),
    }


def validate_snapshot(value: object, *, expected_studio: str | None = None) -> dict | None:
    """Return the allowlisted activity contract or ``None`` for incompatibility.

    A malformed optional activity endpoint must never alter Studio health.
    Empty active/latest values are valid, while a present malformed job makes
    the complete snapshot unusable rather than guessing at its meaning.
    """
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        return None
    studio = value.get("studio")
    observed_at = _finite(value.get("observed_at"), minimum=0)
    if studio not in {"image", "voice"} or observed_at is None or (
        expected_studio is not None and studio != expected_studio
    ):
        return None
    result = {"schema": SCHEMA, "studio": studio, "observed_at": observed_at}
    for key in ("active", "latest"):
        raw = value.get(key)
        item = _job(raw, studio=studio)
        if raw is not None and item is None:
            return None
        if item and ((key == "active" and item["state"] not in ACTIVE_STATES) or
                     (key == "latest" and item["state"] not in TERMINAL_STATES)):
            return None
        if item and any(item.get(name) is not None and item[name] > observed_at
                        for name in ("updated_at", "finished_at")):
            return None
        result[key] = item
    return result


def _hub_origin_device(settings: dict) -> str:
    prefix = "Studio Hub KH · "
    site_name = _text(settings.get("site_name"), _MAX_ORIGIN_DEVICE - len(prefix))
    return f"{prefix}{site_name}" if site_name else "Studio Hub KH"


def _broker_jobs(registry: list[dict], batches: dict,
                 *, hub_origin_device: str | None = None) -> dict[str, list[dict]]:
    if hub_origin_device is None:
        hub_origin_device = _hub_origin_device(control_plane.load_settings())
    by_id = {studio.get("id"): studio for studio in registry}
    result: dict[str, list[dict]] = defaultdict(list)
    for batch in (batches or {}).values():
        if not isinstance(batch, dict):
            continue
        model = _text(batch.get("model"), _MAX_MODEL)
        for item in batch.get("items") or []:
            if not isinstance(item, dict):
                continue
            job_id = _text(
                item.get("studio_job_id") or item.get("studio_task_id"), _MAX_ID,
            )
            studio_id = item.get("studio")
            studio = by_id.get(studio_id)
            state = item.get("state")
            if not job_id or studio is None or state not in VALID_STATES:
                continue
            default_operation = "image" if studio.get("modality") == "image" else "speech"
            operation = batch.get("operation", default_operation)
            if operation not in VALID_OPERATIONS:
                continue
            result[job_id].append({
                "id": job_id, "state": state, "studio": studio_id,
                "machine": studio.get("machine", "local"),
                "model": model, "operation": operation, "source": "job",
                "progress": _finite(item.get("progress"), minimum=0),
                "origin": "hub", "origin_device": hub_origin_device,
                "started_at": _finite(item.get("started_at"), minimum=0),
                "finished_at": _finite(item.get("finished_at"), minimum=0),
            })
    return result


def _reachable(status: str | None) -> bool:
    return status in {"up", "degraded"}


def _reporter_support(status: dict, now: float) -> str | None:
    """Classify direct evidence without treating rejected clock data as empty."""
    snapshot = validate_snapshot(status.get("activity"))
    received_at = _finite(status.get("activity_received_at"), minimum=0)
    if snapshot and received_at is not None and (
        abs(snapshot["observed_at"] - received_at) > REPORTER_CLOCK_SKEW_S
    ):
        return "skew"
    return status.get("activity_support")


def _observed_jobs(registry: list[dict], statuses: dict, batches: dict,
                   now: float, conn=None) -> list[dict]:
    """Merge Studio observations with broker ownership without double-counting."""
    hub_origin_device = _hub_origin_device(control_plane.load_settings())
    broker_jobs = _broker_jobs(registry, batches, hub_origin_device=hub_origin_device)
    result = []
    seen: set[tuple[str, str, str, str]] = set()
    for studio in registry:
        studio_id = studio.get("id")
        status = statuses.get(studio_id) or {}
        snapshot = validate_snapshot(status.get("activity"), expected_studio=studio.get("modality"))
        if not snapshot:
            continue
        received_at = _finite(status.get("activity_received_at"), minimum=0)
        # Older cached status dictionaries have no receipt field; their caller
        # is this controller poll, so use its injected controller time instead.
        received_at = now if received_at is None else received_at
        if abs(snapshot["observed_at"] - received_at) > REPORTER_CLOCK_SKEW_S:
            continue
        for key in ("active", "latest"):
            job = snapshot.get(key)
            if not job:
                continue
            # A cached reporter snapshot is history, not a current lease.  An
            # active entry needs current health + a fresh, supported response.
            if key == "active" and not (
                _reachable(status.get("status")) and
                status.get("activity_support") == "available" and
                0 <= now - received_at <= ACTIVE_FRESH_S
            ):
                continue
            # Repeated terminal snapshots must not resurrect a transition once
            # it aged beyond the fixed controller retention window.
            # Account for allowed worker clock skew before controller pruning:
            # stop accepting before the immutable receipt can be deleted.
            if key == "latest" and now - job.get("finished_at", now) >= RETENTION_S - REPORTER_CLOCK_SKEW_S:
                continue
            owned = next((row for row in broker_jobs.get(job["id"], [])
                          if row["studio"] == studio_id), None)
            row = {
                **job,
                "machine": studio.get("machine", "local"),
                "studio": studio_id,
                "observed_at": received_at,
                "activity_received_at": received_at,
                "reported_at": snapshot["observed_at"],
            }
            if owned:
                row["source"] = "job"
                row["model"] = owned.get("model") or row["model"]
                row["origin"] = "hub"
                row["origin_device"] = hub_origin_device
            elif ledger.activity_job_is_hub_owned(
                    row["machine"], studio_id, row["id"], conn=conn):
                # The broker may have released a completed batch before the
                # worker's terminal snapshot arrives. Its earlier recorded
                # studio_job_id remains the authoritative ownership proof.
                row["source"] = "job"
                row["origin"] = "hub"
                row["origin_device"] = hub_origin_device
            marker = (row["machine"], studio_id, row["id"], row["state"])
            if marker not in seen:
                result.append(row)
                seen.add(marker)
    # A broker job may be in flight while an older Studio lacks the optional
    # reporter. Its own durable studio_job_id is still strong live evidence.
    for rows in broker_jobs.values():
        for row in rows:
            if row["state"] not in ACTIVE_STATES:
                continue
            marker = (row["machine"], row["studio"], row["id"], row["state"])
            if marker in seen:
                continue
            result.append({**row, "observed_at": now, "activity_received_at": now,
                           "reported_at": now})
            seen.add(marker)
    return result


def _machine_groups(registry: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for studio in registry:
        if studio.get("modality") in {"image", "voice"}:
            grouped[studio.get("machine", "local")].append(studio)
    return grouped


def _state_at(machine: str, statuses: dict, studios: list[dict], live: list[dict],
              events: list[dict], now: float) -> tuple[str, float, dict | None, dict | None]:
    status_rows = [statuses.get(studio.get("id")) or {} for studio in studios]
    reachable = any(_reachable(row.get("status")) for row in status_rows)
    all_down = bool(status_rows) and all(row.get("status") == "down" for row in status_rows)
    health_problem = any(row.get("status") == "degraded" for row in status_rows) or (
        reachable and any(row.get("status") == "down" for row in status_rows)
    )
    active = [row for row in live if row["machine"] == machine and row["state"] in ACTIVE_STATES]
    active.sort(key=lambda row: (row["state"] != "running", -(row.get("updated_at") or row.get("observed_at") or 0)))
    terminal = [row for row in events if row["state"] in TERMINAL_STATES]
    terminal.sort(key=lambda row: -(row.get("finished_at") or row.get("observed_at") or 0))
    latest = terminal[0] if terminal else None
    latest_time = ((latest or {}).get("finished_at") or (latest or {}).get("observed_at"))
    recent_error = latest if latest and latest["state"] == "error" else None
    if health_problem or recent_error:
        return "needs_attention", now, active[0] if active else None, latest
    if active:
        current = active[0]
        return "working", now, current, latest
    if all_down:
        return "offline", now, None, latest
    if latest and latest["state"] in {"done", "cancelled"} and latest_time is not None:
        age = max(0.0, now - latest_time)
        if latest["state"] == "done" and age < JUST_FINISHED_S:
            return "just_finished", latest_time, None, latest
        if age >= LONG_IDLE_S:
            return "long_idle", latest_time, None, latest
        return "ready", latest_time, None, latest
    support = {_reporter_support(row, now) for row in status_rows}
    if reachable and support == {"available"}:
        return "ready", now, None, latest
    return "unknown", now, None, latest


def _utilization(machine: str, since_s: float, now: float, *, partial: bool,
                 active_since: float | None = None) -> dict:
    if active_since is not None:
        since_s = max(since_s, active_since)
    rows = ledger.machine_state_transitions(
        machine, before_s=now, since_s=active_since,
    )
    before = [row for row in rows if row["observed_at"] <= since_s]
    after = [row for row in rows if row["observed_at"] > since_s]
    if not before:
        return {"ratio": None, "evidence": "partial"}
    current = before[-1]
    cursor = since_s
    reachable_s = working_s = 0.0
    for row in [*after, {**current, "observed_at": now}]:
        end = min(now, row["observed_at"])
        duration = max(0.0, end - cursor)
        if current["reachable"]:
            reachable_s += duration
            if current["working"]:
                working_s += duration
        current, cursor = row, end
    ratio = round(working_s / reachable_s, 4) if reachable_s else None
    return {"ratio": ratio, "evidence": "partial" if partial else "complete"}


def _performance(machine: str, events: list[dict], live: dict | None,
                 latest: dict | None, since_s: float) -> tuple[float | None, dict | None]:
    usable = [row for row in events if row["state"] == "done"
              and row.get("runtime_s") is not None and row["runtime_s"] > 0
              and row.get("finished_at", row.get("observed_at", 0)) >= since_s]
    groups: dict[tuple[str, str | None, str], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in usable:
        family = row["studio"].split("@", 1)[0]
        groups[(family, row.get("operation"), row["model"])][row["machine"]].append(row["runtime_s"])
    preferred = live or latest or {}
    preferred_model = preferred.get("model")
    preferred_operation = preferred.get("operation")
    candidates = [(key, values) for key, values in groups.items()
                  if (preferred_model is None or key[2] == preferred_model)
                  and (preferred_operation is None or key[1] == preferred_operation)]
    if not candidates:
        return None, None
    key, values = max(candidates, key=lambda item: len(item[1].get(machine, ())) )
    mine = values.get(machine, [])
    if len(mine) < 3:
        return None, None
    own_median = round(float(statistics.median(mine)), 2)
    machine_medians = [statistics.median(runtimes) for runtimes in values.values()
                       if len(runtimes) >= 3]
    if len(machine_medians) < 2:
        return own_median, None
    fleet_median = float(statistics.median(machine_medians))
    if fleet_median <= 0:
        return own_median, None
    return own_median, {
        "fleet_median_s": round(fleet_median, 2),
        "percent_faster": round((fleet_median - own_median) / fleet_median * 100, 1),
        "studio": key[0], "operation": key[1], "model": key[2],
    }


def observe_poll(registry: list[dict], statuses: dict, batches: dict,
                 now: float | None = None) -> None:
    """Persist one poll's meaningful job and machine transitions."""
    now = float(time.time() if now is None else now)
    with ledger.activity_transaction() as conn:
        live = _observed_jobs(registry, statuses, batches, now, conn=conn)
        for row in live:
            ledger.record_activity_event(
                machine=row["machine"], studio=row["studio"], job_id=row["id"],
                state=row["state"], model=row.get("model"), source=row["source"],
                operation=row.get("operation"),
                origin=row.get("origin"), origin_device=row.get("origin_device"),
                progress=row.get("progress"), started_at=row.get("started_at"),
                finished_at=row.get("finished_at"), runtime_s=row.get("runtime_s"),
                error_code=row.get("error_code"), reported_at=row.get("reported_at"),
                activity_received_at=row.get("activity_received_at", now),
                hub_owned=row["source"] == "job", observed_at=row.get("observed_at", now),
                conn=conn,
            )
        for machine, studios in _machine_groups(registry).items():
            status_rows = [statuses.get(studio.get("id")) or {} for studio in studios]
            reachable = any(_reachable(row.get("status")) for row in status_rows)
            working = any(row["machine"] == machine and row["state"] in ACTIVE_STATES
                          for row in live)
            state, _, _, _ = _state_at(
                machine, statuses, studios, live,
                ledger.activity_events(
                    machine=machine, since_s=now - RETENTION_S, conn=conn,
                ), now,
            )
            ledger.record_machine_state(
                machine=machine, reachable=reachable, working=working,
                state=state, observed_at=now, conn=conn,
            )
        ledger.prune_activity(now - RETENTION_S, conn=conn)


def fleet_snapshot(registry: list[dict], statuses: dict, batches: dict,
                   since_s: float | None, now: float | None = None) -> dict:
    """Return current operator state plus bounded 30-day performance evidence."""
    now = float(time.time() if now is None else now)
    since_s = max(now - RETENTION_S, float(since_s if since_s is not None else now - RETENTION_S))
    grouped = _machine_groups(registry)
    live = _observed_jobs(registry, statuses, batches, now)
    all_events = ledger.activity_events(since_s=now - RETENTION_S)
    epochs = {
        machine: ledger.machine_registration_epoch(machine)
        for machine in grouped
    }
    active_events = [
        row for row in all_events
        if row["machine"] in grouped
        and (not (epoch := epochs.get(row["machine"]))
             or row["observed_at"] >= epoch["active_since"])
    ]
    rows, pulse = [], {state: 0 for state in (
        "working", "just_finished", "ready", "long_idle", "offline", "needs_attention", "unknown",
    )}
    for machine, studios in grouped.items():
        epoch = epochs.get(machine)
        active_since = epoch["active_since"] if epoch else None
        events = [row for row in active_events if row["machine"] == machine]
        state, fallback_since, current, latest = _state_at(
            machine, statuses, studios, live, events, now,
        )
        status_rows = [statuses.get(studio.get("id")) or {} for studio in studios]
        support = {_reporter_support(row, now) for row in status_rows}
        limitation = None
        complete = support == {"available"}
        if "skew" in support:
            limitation = "Activity reporter clock skew exceeds policy"
        elif support == {"unavailable"}:
            limitation = "Direct activity unavailable"
        elif "error" in support:
            limitation = "Activity reporter temporarily unavailable"
        elif not support or support == {None}:
            limitation = "Activity evidence pending"
        elif not complete:
            limitation = "Direct activity partially unavailable"
        transitions = ledger.machine_state_transitions(
            machine, before_s=now, since_s=active_since,
        )
        previous = next((row for row in reversed(transitions) if row["state"] == state), None)
        state_since = (previous or {}).get("state_since")
        if state_since is None:
            state_since = fallback_since
        completed = sum(1 for row in events if row["state"] == "done"
                        and (row.get("finished_at") or row["observed_at"]) >= since_s)
        failed = sum(1 for row in events if row["state"] == "error"
                     and (row.get("finished_at") or row["observed_at"]) >= since_s)
        median_runtime_s, relative = _performance(
            machine, active_events, current, latest, max(since_s, active_since or since_s),
        )
        timeline = [row for row in events if row["activity_received_at"] >= since_s][:20]
        row = {
            "machine": machine, "state": state, "state_since": state_since,
            "state_duration_s": round(max(0.0, now - state_since), 1),
            "studio": (current or latest or {}).get("studio"),
            "model": (current or latest or {}).get("model"),
            "operation": (current or latest or {}).get("operation"),
            "job_id": (current or latest or {}).get("job_id") or (current or latest or {}).get("id"),
            "source": (current or latest or {}).get("source"),
            "origin": (current or latest or {}).get("origin", "unknown"),
            "origin_device": (current or latest or {}).get("origin_device"),
            "progress": (current or {}).get("progress"),
            "last_activity_at": ((current or latest or {}).get("updated_at")
                                 or (current or latest or {}).get("finished_at")
                                 or (current or latest or {}).get("observed_at")),
            "latest": latest, "completed": completed, "failed": failed,
            "median_runtime_s": median_runtime_s, "relative_performance": relative,
            "utilization": _utilization(
                machine, since_s, now,
                partial=(not complete),
                active_since=active_since,
            ),
            "limitation": limitation, "timeline": timeline,
        }
        rows.append(row)
        pulse[state] += 1
    priority = {"needs_attention": 0, "working": 1, "just_finished": 2,
                "ready": 3, "long_idle": 4, "offline": 5, "unknown": 6}
    rows.sort(key=lambda row: (priority[row["state"]], row["machine"]))
    return {
        "schema": "studiohub.fleet_activity.v1", "observed_at": now,
        "window": {"since_s": since_s, "now": now}, "pulse": pulse,
        "machines": rows,
    }
