"""Restart-safe Chat Studio work queue for adaptive scene-prompt packs."""

import asyncio
import hashlib
import json
import re
import sqlite3
import time
import uuid

import httpx
from fastapi import HTTPException

from . import broker, ledger
from .peers import studio_request
from .registry import machine_enabled, studio_enabled

MAX_PACKS = 500
MAX_SCENES_PER_PACK = 10
MAX_PAID_CLOUD_SCENES_PER_PACK = 30
MAX_TOTAL_SCENES = 5000
MAX_MESSAGE_CHARS = 500_000
MAX_TRIES = 3
TRANSIENT_RETRY_DELAYS = (5, 15)
TERMINAL_STATES = {"done", "partial", "error", "cancelled"}

batches: dict[str, dict] = {}
busy_studios: set[str] = set()
_pack_tasks: dict[tuple[str, int], asyncio.Task] = {}
_dispatcher_task: asyncio.Task | None = None
_wakeup = asyncio.Event()
_shutting_down = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_batches (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  finished INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_created ON chat_batches(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_idempotency ON chat_batches(idempotency_key, finished);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(ledger.DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _save(batch: dict) -> None:
    batch["updated_at"] = time.time()
    finished = int(all(pack["state"] in TERMINAL_STATES for pack in batch["packs"]))
    if finished and not batch.get("finished_at"):
        batch["finished_at"] = batch["updated_at"]
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chat_batches "
            "(id,created_at,updated_at,finished,idempotency_key,payload) VALUES (?,?,?,?,?,?)",
            (batch["id"], batch["created_at"], batch["updated_at"], finished,
             batch["idempotency_key"], json.dumps(batch)),
        )


def _load_rows(where: str = "", params: tuple = ()) -> list[dict]:
    sql = "SELECT payload FROM chat_batches"
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY created_at DESC"
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [json.loads(row[0]) for row in rows]


def get_batch(batch_id: str) -> dict | None:
    if batch_id in batches:
        return batches[batch_id]
    rows = _load_rows("id = ?", (batch_id,))
    return rows[0] if rows else None


def _identifier(value: object, field: str, max_length: int = 160) -> str:
    clean = str(value or "").strip()
    if (not clean or len(clean) > max_length or clean in {".", ".."}
            or "/" in clean or "\\" in clean
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ :+-]*", clean)):
        raise HTTPException(400, f"invalid {field}")
    return clean


def _model(value: object) -> str:
    clean = str(value or "").strip()
    if (not clean or len(clean) > 240 or clean.startswith("/") or "\\" in clean
            or ".." in clean.split("/")
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", clean)):
        raise HTTPException(400, "invalid model")
    return clean


def _model_cost_tier(value: object) -> str:
    tier = str(value or "local").strip().lower()
    if tier not in {"local", "free", "paid"}:
        raise HTTPException(400, "model_cost_tier must be local, free, or paid")
    return tier


def _messages(value: object) -> list[dict]:
    if not isinstance(value, list) or not value or len(value) > 20:
        raise HTTPException(400, "each pack needs 1 to 20 messages")
    out = []
    total = 0
    for message in value:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
            raise HTTPException(400, "message role must be system, user, or assistant")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(400, "message content must be non-empty text")
        total += len(content)
        out.append({"role": message["role"], "content": content})
    if total > MAX_MESSAGE_CHARS:
        raise HTTPException(413, "pack messages are too large")
    return out


_ALLOWED_PARAMS = {
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "seed",
    "stop", "frequency_penalty", "presence_penalty", "response_format",
}


def _params(value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(400, "pack params must be an object")
    unknown = set(value) - _ALLOWED_PARAMS
    if unknown:
        raise HTTPException(400, f"unsupported Chat parameter(s): {', '.join(sorted(unknown))}")
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "pack params must be JSON serializable")
    return value


def _idempotency(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_batch(payload: dict) -> tuple[dict, bool]:
    if not isinstance(payload, dict):
        raise HTTPException(400, "request body must be an object")
    raw_packs = payload.get("packs")
    if not isinstance(raw_packs, list) or not raw_packs or len(raw_packs) > MAX_PACKS:
        raise HTTPException(400, f"packs must contain 1 to {MAX_PACKS} entries")
    model = _model(payload.get("model"))
    model_cost_tier = _model_cost_tier(payload.get("model_cost_tier"))
    max_scenes_per_pack = (MAX_PAID_CLOUD_SCENES_PER_PACK
                           if model_cost_tier == "paid" else MAX_SCENES_PER_PACK)
    kind = str(payload.get("kind") or "visual").strip().lower()
    if kind not in {"visual", "motion"}:
        raise HTTPException(400, "kind must be visual or motion")
    label = _identifier(payload["label"], "label") if payload.get("label") else None
    project = _identifier(payload["project"], "project") if payload.get("project") else None
    episode = _identifier(payload["episode"], "episode") if payload.get("episode") else None

    packs = []
    all_scene_ids: set[str] = set()
    for index, raw in enumerate(raw_packs):
        if not isinstance(raw, dict):
            raise HTTPException(400, "each pack must be an object")
        pack_id = _identifier(raw.get("pack_id") or f"pack-{index + 1}", "pack_id")
        scene_values = raw.get("scene_ids")
        if (not isinstance(scene_values, list) or not scene_values
                or len(scene_values) > max_scenes_per_pack):
            raise HTTPException(400, f"each {model_cost_tier} pack must contain 1 to "
                                f"{max_scenes_per_pack} scene_ids")
        scene_ids = [_identifier(scene_id, "scene_id") for scene_id in scene_values]
        if len(set(scene_ids)) != len(scene_ids):
            raise HTTPException(400, f"scene_ids must be unique in {pack_id}")
        overlap = all_scene_ids.intersection(scene_ids)
        if overlap:
            raise HTTPException(400, f"scene_id appears in more than one pack: {sorted(overlap)[0]}")
        all_scene_ids.update(scene_ids)
        packs.append({
            "index": index, "pack_id": pack_id, "scene_ids": scene_ids,
            "messages": _messages(raw.get("messages")), "params": _params(raw.get("params")),
            "state": "queued", "tries": 0, "studio": None,
            "duration_seconds": None, "error": None, "results": {},
            "raw_responses": [], "started_at": None, "finished_at": None,
            "retry_at": None,
        })
    if len(all_scene_ids) > MAX_TOTAL_SCENES:
        raise HTTPException(400, f"batch exceeds {MAX_TOTAL_SCENES} total scenes")

    canonical = {
        "model": model, "model_cost_tier": model_cost_tier,
        "kind": kind, "label": label, "project": project,
        "episode": episode,
        "packs": [{"pack_id": p["pack_id"], "scene_ids": p["scene_ids"],
                   "messages": p["messages"], "params": p["params"]} for p in packs],
    }
    key = _idempotency(canonical)
    existing = _load_rows("idempotency_key = ? AND finished = 0", (key,))
    if existing:
        batch = existing[0]
        batches.setdefault(batch["id"], batch)
        return batch, True

    now = time.time()
    batch = {
        "id": uuid.uuid4().hex[:12], "idempotency_key": key,
        "created_at": now, "updated_at": now, "finished_at": None,
        "cancelled": False, "model": model, "model_cost_tier": model_cost_tier,
        "kind": kind, "label": label,
        "project": project, "episode": episode, "packs": packs,
    }
    batches[batch["id"]] = batch
    _save(batch)
    _wakeup.set()
    return batch, False


def _strip_fence(content: str) -> str:
    clean = content.strip()
    clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s*```$", "", clean)
    return clean.strip()


def parse_scene_results(content: str, expected: list[str], kind: str) -> dict[str, str]:
    """Extract stable scene-id results from the accepted compact JSON shapes."""
    try:
        data = json.loads(_strip_fence(content))
    except (json.JSONDecodeError, TypeError):
        if len(expected) == 1 and content.strip():
            return {expected[0]: content.strip()}
        # Small local models sometimes prepend a thought channel, and a tight
        # output limit may truncate the outer results array after several
        # complete rows. Recover every self-contained JSON scene object so
        # valid work is saved and the next try requests only missing IDs.
        decoder = json.JSONDecoder()
        recovered = []
        for match in re.finditer(r"\{", content):
            try:
                row, _ = decoder.raw_decode(content[match.start():])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(row, dict) and (row.get("scene_id") or row.get("id")):
                recovered.append(row)
        if not recovered:
            return {}
        data = recovered
    if isinstance(data, dict):
        candidate = data.get("results", data.get("prompts", data))
    else:
        candidate = data
    out: dict[str, str] = {}
    allowed = set(expected)
    if isinstance(candidate, dict):
        for scene_id, text in candidate.items():
            if scene_id in allowed and isinstance(text, str) and text.strip():
                out[scene_id] = text.strip()
    elif isinstance(candidate, list):
        text_keys = ("motion_prompt", "prompt", "text") if kind == "motion" else ("visual_prompt", "prompt", "text")
        for row in candidate:
            if not isinstance(row, dict):
                continue
            scene_id = row.get("scene_id") or row.get("id")
            text = next((row.get(key) for key in text_keys if isinstance(row.get(key), str)), None)
            if scene_id in allowed and text and text.strip():
                out[scene_id] = text.strip()
    return out


def summary(batch: dict, include_raw: bool = False, include_results: bool = True) -> dict:
    states = [pack["state"] for pack in batch["packs"]]
    counts = {state: states.count(state) for state in ("queued", "running", "done", "partial", "error", "cancelled")}
    if counts["queued"] or counts["running"]:
        status = "running" if counts["running"] else "queued"
    elif counts["error"] or counts["partial"]:
        status = "partial" if counts["done"] or counts["partial"] else "error"
    elif counts["cancelled"]:
        status = "partial" if counts["done"] else "cancelled"
    else:
        status = "done"
    result = {
        "id": batch["id"], "status": status, "kind": batch["kind"],
        "model": batch["model"], "model_cost_tier": batch.get("model_cost_tier", "local"),
        "label": batch.get("label"),
        "project": batch.get("project"), "episode": batch.get("episode"),
        "created_at": batch["created_at"], "updated_at": batch.get("updated_at"),
        "finished_at": batch.get("finished_at"), "total_packs": len(states), **counts,
        "queue_note": batch.get("queue_note"), "max_tries": MAX_TRIES,
        "total_scenes": sum(len(pack["scene_ids"]) for pack in batch["packs"]),
        "completed_scenes": sum(len(pack.get("results", {})) for pack in batch["packs"]),
        "duration_seconds": round(sum(pack.get("duration_seconds") or 0 for pack in batch["packs"]), 2),
    }
    result["packs"] = []
    for pack in batch["packs"]:
        row = {
            "index": pack["index"], "pack_id": pack["pack_id"],
            "scene_ids": pack["scene_ids"], "state": pack["state"],
            "studio": pack.get("studio"), "tries": pack["tries"],
            "max_tries": MAX_TRIES, "started_at": pack.get("started_at"),
            "retry_at": pack.get("retry_at"),
            "duration_seconds": pack.get("duration_seconds"), "error": pack.get("error"),
            "missing_scene_ids": [scene_id for scene_id in pack["scene_ids"]
                                  if scene_id not in pack.get("results", {})],
        }
        if include_results:
            row["results"] = [
                {"scene_id": scene_id, "text": pack.get("results", {}).get(scene_id)}
                for scene_id in pack["scene_ids"] if scene_id in pack.get("results", {})
            ]
        if include_raw:
            row["raw_responses"] = pack.get("raw_responses", [])
        result["packs"].append(row)
    return result


def active_assignments() -> dict[str, dict]:
    """Current Chat lease details keyed by Studio id for the live dashboard."""
    active = {}
    for batch in batches.values():
        for pack in batch["packs"]:
            studio = pack.get("studio")
            if pack["state"] == "running" and studio:
                active[studio] = {
                    "kind": "chat", "batch_id": batch["id"],
                    "project": batch.get("project"), "episode": batch.get("episode"),
                    "pack_id": pack["pack_id"], "attempt": pack["tries"],
                    "max_attempts": MAX_TRIES, "started_at": pack.get("started_at"),
                }
    return active


def list_batches() -> list[dict]:
    persisted = {batch["id"]: batch for batch in _load_rows()}
    persisted.update(batches)
    return [summary(batch, include_results=False) for batch in sorted(
        persisted.values(), key=lambda row: row["created_at"], reverse=True)]


def _is_terminal(batch: dict) -> bool:
    return all(pack["state"] in TERMINAL_STATES for pack in batch["packs"])


def remove_batch(batch_id: str) -> bool:
    """Delete ONE finished batch (done/partial/error/cancelled) from memory + DB.
    Returns False if it's still active (cancel it first) or unknown."""
    batch = get_batch(batch_id)
    if not batch or not _is_terminal(batch):
        return False
    batches.pop(batch_id, None)
    for index in range(len(batch["packs"])):
        _pack_tasks.pop((batch_id, index), None)
    with _conn() as conn:
        conn.execute("DELETE FROM chat_batches WHERE id = ?", (batch_id,))
    return True


def clear_terminal() -> int:
    """Remove every finished batch from memory + DB (running/queued ones are
    kept). Returns how many were removed."""
    for bid, batch in list(batches.items()):
        if _is_terminal(batch):
            batches.pop(bid, None)
            for index in range(len(batch["packs"])):
                _pack_tasks.pop((bid, index), None)
    with _conn() as conn:
        cur = conn.execute("DELETE FROM chat_batches WHERE finished = 1")
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


async def _eligible_studios(monitor, model: str) -> list[dict]:
    eligible = []
    for studio in monitor.registry:
        if (studio.get("modality") != "chat" or studio["id"] in busy_studios
                or broker.in_maintenance(studio["id"])):
            continue
        machine = studio.get("machine", "local")
        if (monitor.status.get(studio["id"], {}).get("status") != "up"
                or not machine_enabled(machine)
                or not studio_enabled(machine, studio["id"])
                or machine in broker.busy_machines()):
            continue
        catalog = await monitor.get_catalog(studio)
        entry = next((item for item in (catalog or {}).get("models", [])
                      if item.get("repo") == model or model in (item.get("aliases") or [])), None)
        if entry and (entry.get("is_cloud") or broker.is_cached(entry)):
            eligible.append(studio)
    return eligible


async def dispatch_once(monitor) -> int:
    active = sorted(
        (batch for batch in batches.values() if not batch.get("cancelled")),
        key=lambda batch: batch["created_at"],
    )
    for position, batch in enumerate(active):
        now = time.time()
        queued_all = [pack for pack in batch["packs"] if pack["state"] == "queued"]
        queued = [pack for pack in queued_all if not pack.get("retry_at")
                  or pack["retry_at"] <= now]
        if not queued:
            retry_times = [pack["retry_at"] for pack in queued_all if pack.get("retry_at")]
            batch["queue_note"] = (f"Automatic retry in {max(1, round(min(retry_times) - now))}s"
                                   if retry_times else None)
            continue
        eligible = await _eligible_studios(monitor, batch["model"])
        if not eligible:
            batch["queue_note"] = "Waiting for a free online Chat Studio with this model cached"
            continue

        assigned = 0
        batch["queue_note"] = None
        for studio, pack in zip(eligible, queued):
            machine = studio.get("machine", "local")
            owner = f"chat:{batch['id']}:{pack['index']}"
            if not broker.acquire_external_machine(machine, owner):
                continue
            pack.update(state="running", studio=studio["id"], error=None, retry_at=None,
                        started_at=time.time(), tries=pack["tries"] + 1)
            busy_studios.add(studio["id"])
            task = asyncio.create_task(_run_pack(monitor, batch, pack, studio, owner))
            _pack_tasks[(batch["id"], pack["index"])] = task
            assigned += 1
        if assigned:
            _save(batch)
            older = batch.get("episode") or batch.get("project") or batch["id"]
            for waiting in active[position + 1:]:
                if any(pack["state"] == "queued" for pack in waiting["packs"]):
                    waiting["queue_note"] = f"Waiting behind older batch {older}"
            return assigned
    return 0


async def _run_pack(monitor, batch: dict, pack: dict, studio: dict, owner: str) -> None:
    started = time.time()
    try:
        missing = [scene_id for scene_id in pack["scene_ids"] if scene_id not in pack["results"]]
        messages = list(pack["messages"])
        if pack["results"]:
            messages.append({
                "role": "user",
                "content": "Your previous response was incomplete. Return JSON for ONLY these missing "
                           f"scene IDs: {json.dumps(missing)}. Do not repeat completed IDs.",
            })
        body = {"model": batch["model"], "messages": messages, "stream": False, **pack["params"]}
        url, headers = studio_request(studio, "/v1/chat/completions")
        response = await monitor._client.post(
            url, json=body, headers=headers,
            timeout=httpx.Timeout(connect=5, read=600, write=30, pool=5),
        )
        if response.status_code >= 400:
            error = RuntimeError(f"HTTP {response.status_code}: {response.text[:500] or 'Chat completion failed'}")
            error.transient = response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
            raise error
        payload = response.json()
        content = str(payload.get("choices", [{}])[0].get("message", {}).get("content") or "")
        if not content.strip():
            raise ValueError("Chat Studio returned empty content")
        pack["raw_responses"] = [*(pack.get("raw_responses") or []), content][-MAX_TRIES:]
        pack["results"].update(parse_scene_results(content, missing, batch["kind"]))
        remaining = [scene_id for scene_id in pack["scene_ids"] if scene_id not in pack["results"]]
        pack["duration_seconds"] = round(float(payload.get("elapsed_seconds") or time.time() - started), 2)
        if not remaining:
            pack.update(state="done", error=None, finished_at=time.time())
        elif pack["tries"] < MAX_TRIES and not batch.get("cancelled"):
            pack.update(state="queued", studio=None,
                        error=f"incomplete response: {len(remaining)} scene(s) still missing")
        else:
            pack.update(state="partial" if pack["results"] else "error",
                        error=f"incomplete response after {pack['tries']} tries: "
                              f"{len(remaining)} scene(s) missing", finished_at=time.time())
    except asyncio.CancelledError:
        if _shutting_down and not batch.get("cancelled"):
            pack.update(state="queued", studio=None, error="interrupted by Hub shutdown")
        else:
            pack.update(state="cancelled", error="cancelled", finished_at=time.time())
    except Exception as exc:
        transient = isinstance(exc, (httpx.HTTPError, OSError, ValueError)) or getattr(exc, "transient", False)
        if transient and pack["tries"] < MAX_TRIES and not batch.get("cancelled"):
            delay = TRANSIENT_RETRY_DELAYS[min(pack["tries"] - 1,
                                               len(TRANSIENT_RETRY_DELAYS) - 1)]
            pack.update(state="queued", studio=None,
                        retry_at=time.time() + delay,
                        error=f"try {pack['tries']} failed; retrying in {delay}s: {exc}")
        else:
            pack.update(state="partial" if pack["results"] else "error",
                        error=str(exc), finished_at=time.time())
    finally:
        busy_studios.discard(studio["id"])
        broker.release_external_machine(studio.get("machine", "local"), owner)
        _pack_tasks.pop((batch["id"], pack["index"]), None)
        _save(batch)
        _wakeup.set()


async def _dispatch_loop(monitor) -> None:
    while True:
        try:
            assigned = await dispatch_once(monitor)
            _wakeup.clear()
            try:
                await asyncio.wait_for(_wakeup.wait(), timeout=0.1 if assigned else 2.0)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(2)


def restore_batches() -> int:
    restored = 0
    for batch in _load_rows("finished = 0"):
        for pack in batch["packs"]:
            if pack["state"] == "running":
                pack.update(state="queued", studio=None, retry_at=None,
                            error="interrupted by Hub restart")
        batches[batch["id"]] = batch
        _save(batch)
        restored += 1
    if restored:
        _wakeup.set()
    return restored


def start_dispatcher(monitor) -> None:
    global _dispatcher_task, _shutting_down
    _shutting_down = False
    loop = asyncio.get_running_loop()
    if _dispatcher_task is None or _dispatcher_task.done() or _dispatcher_task.get_loop() is not loop:
        _dispatcher_task = asyncio.create_task(_dispatch_loop(monitor))


async def stop() -> None:
    global _dispatcher_task, _shutting_down
    _shutting_down = True
    tasks = [task for task in (_dispatcher_task, *_pack_tasks.values()) if task and not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _dispatcher_task = None


async def cancel_batch(batch_id: str) -> dict | None:
    batch = get_batch(batch_id)
    if not batch:
        return None
    batch["cancelled"] = True
    for pack in batch["packs"]:
        if pack["state"] == "queued":
            pack.update(state="cancelled", error="cancelled", finished_at=time.time())
        elif pack["state"] == "running":
            task = _pack_tasks.get((batch_id, pack["index"]))
            if task:
                task.cancel()
    batches[batch_id] = batch
    _save(batch)
    _wakeup.set()
    return batch


def retry_batch(batch_id: str) -> tuple[dict | None, int]:
    batch = get_batch(batch_id)
    if not batch:
        return None, 0
    retried = 0
    for pack in batch["packs"]:
        if pack["state"] in {"partial", "error"}:
            pack.update(state="queued", error=None, studio=None, tries=0, retry_at=None,
                        started_at=None, finished_at=None)
            retried += 1
    if retried:
        batch["cancelled"] = False
        batch["finished_at"] = None
        batches[batch_id] = batch
        _save(batch)
        _wakeup.set()
    return batch, retried


def statistics() -> dict:
    rows = _load_rows()
    packs = [pack for batch in rows for pack in batch["packs"]]
    return {
        "batches": len(rows), "packs": len(packs),
        "scenes": sum(len(pack["scene_ids"]) for pack in packs),
        "completed_scenes": sum(len(pack.get("results", {})) for pack in packs),
        "duration_seconds": round(sum(pack.get("duration_seconds") or 0 for pack in packs), 2),
    }


def reset_for_tests() -> None:
    global _dispatcher_task, _shutting_down
    for task in [task for task in (_dispatcher_task, *_pack_tasks.values()) if task and not task.done()]:
        task.cancel()
    batches.clear()
    busy_studios.clear()
    _pack_tasks.clear()
    _dispatcher_task = None
    _shutting_down = False
    broker._external_machine_leases.clear()
