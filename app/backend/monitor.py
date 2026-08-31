"""Health poller + catalog aggregator.

Polls every studio's /api/health on a short interval and caches /api/catalog
with a TTL. Model params/schemas are never normalized — catalog
entries are passed through verbatim, only ANNOTATED with hub_* fields naming
the source studio.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger("studiohub.monitor")

from .registry import DATA_DIR, base_url, load_registry, prune_machine_metadata
from .peers import studio_request

POLL_INTERVAL_S = 5.0
HEALTH_TIMEOUT_S = 3.0
CATALOG_TIMEOUT_S = 10.0
CATALOG_TTL_S = 60.0
CATALOG_REFRESH_INTERVAL_S = 60.0
CATALOG_STALE_S = 5 * 60.0
HEALTH_FAILURES_TO_DOWN = 3
HEALTH_SUCCESSES_TO_RECOVER = 2

_ACTIVITY_JOB_FIELDS = (
    "id", "state", "model", "source", "progress", "created_at",
    "started_at", "updated_at", "finished_at", "runtime_s", "error_code",
    "origin", "origin_device",
)
_ACTIVITY_BATCH_ITEM_FIELDS = (
    "studio_job_id", "studio", "state", "progress", "started_at", "finished_at",
)


def _activity_value(value):
    """Keep the poll handoff scalar-only; params and prompts stay on the loop."""
    return value if value is None or isinstance(value, (str, int, float, bool)) else None


def _activity_job_view(job):
    if not isinstance(job, dict):
        return None
    return {name: _activity_value(job.get(name)) for name in _ACTIVITY_JOB_FIELDS if name in job}


def _activity_snapshot_view(snapshot):
    if not isinstance(snapshot, dict):
        return None
    result = {
        name: _activity_value(snapshot.get(name))
        for name in ("schema", "studio", "observed_at") if name in snapshot
    }
    for name in ("active", "latest"):
        if name in snapshot:
            result[name] = _activity_job_view(snapshot[name])
    return result


def _activity_poll_inputs(registry: list[dict], statuses: dict, batches: dict):
    """Build the tiny, value-only view consumed by the optional activity ledger."""
    registry_view = []
    statuses_view = {}
    for studio in registry:
        if not isinstance(studio, dict):
            continue
        view = {
            name: _activity_value(studio.get(name))
            for name in ("id", "modality", "machine") if name in studio
        }
        registry_view.append(view)
        studio_id = view.get("id")
        status = statuses.get(studio_id)
        if not isinstance(status, dict):
            continue
        status_view = {
            name: _activity_value(status.get(name))
            for name in ("status", "activity_support", "activity_received_at")
            if name in status
        }
        if "activity" in status:
            status_view["activity"] = _activity_snapshot_view(status["activity"])
        statuses_view[studio_id] = status_view

    batches_view = {}
    for batch_id, batch in (batches or {}).items():
        if not isinstance(batch, dict):
            continue
        items = []
        for item in batch.get("items") or ():
            if not isinstance(item, dict):
                continue
            items.append({
                name: _activity_value(item.get(name))
                for name in _ACTIVITY_BATCH_ITEM_FIELDS if name in item
            })
        batches_view[batch_id] = {"model": _activity_value(batch.get("model")), "items": items}
    return registry_view, statuses_view, batches_view


def is_cached(model: dict) -> bool:
    """Whether a studio has this model fully downloaded.

    Every studio reports `cache` as a dict {state: 'cached'|'absent'|'partial'}.
    The trap: bool(cache) is True for ANY non-empty dict, so a naive truthiness
    check marks even 'absent' models as downloaded. Only 'cached' counts."""
    cache = model.get("cache")
    if isinstance(cache, dict):
        return cache.get("state") == "cached"
    return bool(cache)  # tolerate a studio that ever uses a bool/string


# 'render' is a local FFmpeg episode-assembly step. Render studios historically
# flag its catalog entry is_cloud=true only to bypass broker download/memory
# gates, so cloud filtering must exempt it.
LOCAL_ONLY_MODALITIES = {"render"}


class StudioMonitor:
    def __init__(self, *, catalog_state_path: Path | None = None):
        self.registry: list[dict] = load_registry()
        prune_machine_metadata({studio.get("machine", "local")
                                for studio in self.registry})
        self.status: dict[str, dict] = {
            s["id"]: {"status": "unknown", "last_seen": None, "last_checked": None}
            for s in self.registry
        }
        self._catalog_cache: dict[str, tuple[float, dict]] = {}
        self._transcribe_cache: dict[str, tuple[float, dict]] = {}
        self._catalog_meta: dict[str, dict] = {}
        self.catalog_state_path = (
            catalog_state_path or DATA_DIR / "catalog_observations.json"
        )
        self._restart_alerts: dict[str, dict] = {}
        self._client = httpx.AsyncClient()
        self._task: asyncio.Task | None = None
        self._catalog_task: asyncio.Task | None = None
        self._catalog_refresh_lock: asyncio.Lock | None = None
        self._load_catalog_state()

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self):
        self._task = asyncio.create_task(self._poll_loop())
        self._catalog_task = asyncio.create_task(self._catalog_refresh_loop())

    async def stop(self):
        for task in (self._task, self._catalog_task):
            if task:
                task.cancel()
        for task in (self._task, self._catalog_task):
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = None
        self._catalog_task = None
        await self._client.aclose()

    def reload_registry(self):
        """Pick up studios.json edits without a restart."""
        self.registry = load_registry()
        prune_machine_metadata({studio.get("machine", "local")
                                for studio in self.registry})
        for s in self.registry:
            self.status.setdefault(
                s["id"],
                {"status": "unknown", "last_seen": None, "last_checked": None},
            )

    def forget_studios(self, studio_ids: set[str]) -> None:
        """Drop removed Studios from status and all live inventory caches."""
        if not studio_ids:
            return
        self.registry = [studio for studio in self.registry
                         if studio["id"] not in studio_ids]
        for studio_id in studio_ids:
            self.status.pop(studio_id, None)
            self._catalog_cache.pop(studio_id, None)
            self._transcribe_cache.pop(studio_id, None)
            self._catalog_meta.pop(studio_id, None)
            self._restart_alerts.pop(studio_id, None)
        self._save_catalog_state()

    # ── durable catalogue observations ───────────────────────────────────
    def _load_catalog_state(self) -> None:
        try:
            value = json.loads(self.catalog_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if not isinstance(value, dict):
            return
        for sid, row in (value.get("studios") or {}).items():
            if not isinstance(sid, str) or not isinstance(row, dict):
                continue
            catalog = row.get("catalog")
            transcription = row.get("transcription")
            catalog_at = row.get("catalog_observed_at")
            transcription_at = row.get("transcription_observed_at")
            if isinstance(catalog, dict) and isinstance(catalog_at, (int, float)):
                self._catalog_cache[sid] = (float(catalog_at), catalog)
            if (isinstance(transcription, dict)
                    and isinstance(transcription_at, (int, float))):
                self._transcribe_cache[sid] = (
                    float(transcription_at), transcription,
                )
            self._catalog_meta[sid] = {
                "last_attempt_at": row.get("last_attempt_at"),
                "last_success_at": row.get("last_success_at"),
                "last_error": row.get("last_error"),
            }

    def _save_catalog_state(self) -> None:
        studios = {}
        studio_ids = set(self._catalog_cache) | set(self._transcribe_cache) \
            | set(self._catalog_meta)
        for sid in sorted(studio_ids):
            catalog = self._catalog_cache.get(sid)
            transcription = self._transcribe_cache.get(sid)
            meta = self._catalog_meta.get(sid, {})
            studios[sid] = {
                "catalog_observed_at": catalog[0] if catalog else None,
                "catalog": catalog[1] if catalog else None,
                "transcription_observed_at": (
                    transcription[0] if transcription else None
                ),
                "transcription": transcription[1] if transcription else None,
                "last_attempt_at": meta.get("last_attempt_at"),
                "last_success_at": meta.get("last_success_at"),
                "last_error": meta.get("last_error"),
            }
        payload = {"schema_version": 1, "studios": studios}
        self.catalog_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.catalog_state_path.with_name(
            f".{self.catalog_state_path.name}.{os.getpid()}.tmp"
        )
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.catalog_state_path)
            os.chmod(self.catalog_state_path, 0o600)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        return (str(exc).strip() or type(exc).__name__)[:240]

    def catalog_observation(self, studio_id: str, *, transcription=False) -> dict:
        cache = (
            self._transcribe_cache if transcription else self._catalog_cache
        ).get(studio_id)
        observed_at = cache[0] if cache else None
        age = max(0.0, time.time() - observed_at) if observed_at else None
        meta = self._catalog_meta.get(studio_id, {})
        return {
            "observed_at": observed_at,
            "age_seconds": round(age, 3) if age is not None else None,
            "stale": age is None or age > CATALOG_STALE_S,
            "last_attempt_at": meta.get("last_attempt_at"),
            "last_success_at": meta.get("last_success_at"),
            "last_error": meta.get("last_error"),
            "source": "persisted_last_good" if cache else "none",
        }

    async def _catalog_refresh_loop(self) -> None:
        try:
            # Let the quick health loop establish reachability first.
            await asyncio.sleep(POLL_INTERVAL_S)
            while True:
                try:
                    await self.refresh_catalogs()
                except Exception:
                    log.warning("catalog refresh cycle failed (continuing)",
                                exc_info=True)
                await asyncio.sleep(CATALOG_REFRESH_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    async def refresh_catalogs(self, *, force: bool = True) -> dict:
        """Refresh every reachable sibling independently of health polling.

        One global lock prevents overlapping cycles. Per-worker calls remain
        concurrent and individually bounded by CATALOG_TIMEOUT_S.
        """
        if self._catalog_refresh_lock is None:
            self._catalog_refresh_lock = asyncio.Lock()
        if self._catalog_refresh_lock.locked():
            return {"started": False, "reason": "refresh_in_progress"}
        async with self._catalog_refresh_lock:
            results = await asyncio.gather(*(
                self._refresh_studio_catalog(studio, force=force)
                for studio in list(self.registry)
            ))
            self._save_catalog_state()
            return {"started": True, "studios": results}

    async def _refresh_studio_catalog(self, studio: dict, *, force: bool) -> dict:
        sid = studio["id"]
        now = time.time()
        meta = self._catalog_meta.setdefault(sid, {})
        meta["last_attempt_at"] = now
        if self.status.get(sid, {}).get("status") != "up":
            meta["last_error"] = "worker_unreachable"
            return {"studio_id": sid, "refreshed": False,
                    "reason": "worker_unreachable"}
        try:
            catalog = await self.get_catalog(studio, force=force)
            if catalog is None:
                raise RuntimeError("catalog_unavailable")
            if studio.get("modality") == "voice":
                transcription = await self.get_transcription(studio, force=force)
                if transcription is None:
                    raise RuntimeError("transcription_catalog_unavailable")
            if meta.get("last_error"):
                return {"studio_id": sid, "refreshed": False,
                        "reason": meta["last_error"]}
            meta.update(last_success_at=time.time(), last_error=None)
            return {"studio_id": sid, "refreshed": True}
        except Exception as exc:
            meta["last_error"] = self._safe_error(exc)
            return {"studio_id": sid, "refreshed": False,
                    "reason": meta["last_error"]}

    # ── health ───────────────────────────────────────────────────────────
    async def _poll_loop(self):
        while True:
            try:
                await self.poll_all()
            except Exception:
                log.warning("poll cycle failed (continuing)", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL_S)

    async def poll_all(self):
        await asyncio.gather(*(self._poll_one(s) for s in self.registry))
        # Activity is an optional extension of an already successful health
        # probe. Persisting its compact transitions here keeps Stats entirely
        # local and never lets a slow/old reporter affect worker health.
        from . import activity, broker
        try:
            registry, statuses, batches = _activity_poll_inputs(
                self.registry, self.status, broker.batches,
            )
            await asyncio.to_thread(activity.observe_poll, registry, statuses, batches)
        except Exception:
            log.warning("activity observation failed (continuing)", exc_info=True)
        # metrics sample + watchdog revival pass (late import: no cycle)
        from . import metrics, peers
        metrics.on_poll(self.registry, self.status)
        # `check_proxy_health` walks every process on the Mac (name + cmdline
        # per candidate) to find Caddy, and that walk gets slower exactly when
        # the machine is busy. Run it in a worker thread: on the loop it froze
        # this single-worker process for the whole walk, so `/health/live`
        # timed out and the site read as flapping while it was merely busy.
        from . import resources
        await asyncio.to_thread(resources.check_proxy_health)
        # refresh peer-Hub resources in the background (TTL-guarded + in-flight
        # guarded inside) so a slow/offline fleet never stalls the health poll.
        asyncio.create_task(peers.refresh(self.registry, self._client))

    def _note_transition(self, studio: dict, prev: str, new: str):
        """Fire an alert when a studio flips up<->down (ignore first-poll
        'unknown' so we don't alert on startup)."""
        if prev == new or prev == "unknown":
            return
        from . import alerts
        label = studio.get("title", studio["id"])
        machine = studio.get("machine", "local")
        if new == "up" and prev == "down":
            alerts.emit("studio_recovered", f"{label} on {machine} is back up",
                        {"studio": studio["id"], "machine": machine})
        elif new != "up" and prev == "up":
            alerts.emit("studio_down", f"{label} on {machine} went down",
                        {"studio": studio["id"], "machine": machine})

    def _note_restart_health(self, studio: dict, health: dict) -> None:
        """Alert once when a worker reports a repeated-restart condition."""
        snapshot = health.get("restart_health") or health.get("restart_rate")
        if not isinstance(snapshot, dict):
            return
        sid = studio["id"]
        active = bool(snapshot.get("alert"))
        previous = self._restart_alerts.get(sid, {})
        label = studio.get("title", sid)
        machine = studio.get("machine", "local")
        if active and not previous.get("active"):
            from . import alerts
            alerts.emit(
                "worker_restart_rate",
                f"{label} on {machine} is restarting repeatedly",
                {
                    "studio": sid,
                    "machine": machine,
                    "status": snapshot.get("status"),
                    "restarts_1h": snapshot.get("restarts_1h"),
                    "restarts_24h": snapshot.get("restarts_24h"),
                    "restarts_7d": snapshot.get("restarts_7d"),
                    "last_restart_at": snapshot.get("last_restart_at"),
                },
            )
        elif not active and previous.get("active"):
            from . import alerts
            alerts.emit(
                "worker_restart_rate_recovered",
                f"{label} on {machine} restart rate returned to normal",
                {"studio": sid, "machine": machine},
            )
        self._restart_alerts[sid] = {"active": active}

    @staticmethod
    def _has_active_lease(studio_id: str) -> bool:
        """A model server may not answer health while synchronous inference is
        running. Its Hub lease is stronger evidence than a short poll timeout."""
        from . import broker, chat_jobs, transcription_jobs
        return (studio_id in broker.busy_studios()
                or studio_id in chat_jobs.busy_studios
                or studio_id in transcription_jobs.busy_studios)

    async def _poll_one(self, studio: dict):
        sid = studio["id"]
        url = f"{base_url(studio)}/api/health"
        started = time.monotonic()
        now = time.time()
        prev_status = self.status.get(sid, {}).get("status", "unknown")
        try:
            r = await self._client.get(url, timeout=HEALTH_TIMEOUT_S)
            latency_ms = round((time.monotonic() - started) * 1000)
            health = r.json()
            self._note_restart_health(studio, health)
            new_status = "up" if health.get("ok") else "degraded"
            previous = self.status.get(sid, {})
            successes = int(previous.get("consecutive_successes") or 0) + 1
            # A worker that was declared down must answer twice before it
            # rejoins the scheduler. This prevents one lucky probe during a
            # restart/update from immediately receiving customer work.
            recovering = (prev_status == "down" and new_status == "up"
                          and successes < HEALTH_SUCCESSES_TO_RECOVER)
            effective_status = "down" if recovering else new_status
            self.status[sid] = {
                "status": effective_status,
                "latency_ms": latency_ms,
                "app_version": health.get("app_version"),
                "health": health,  # verbatim — includes chat's loaded_model etc.
                "last_seen": now,
                "last_checked": now,
                "consecutive_failures": 0,
                "consecutive_successes": successes,
                "health_recovering": recovering,
                "health_probe_degraded": False,
            }
            # The optional reporter may have one temporary failed request.
            # Retain its last valid observation across every ordinary health
            # refresh so the live board does not falsely forget a job.
            if previous.get("activity") is not None:
                self.status[sid]["activity"] = previous["activity"]
            if previous.get("activity_received_at") is not None:
                self.status[sid]["activity_received_at"] = previous["activity_received_at"]
            if previous.get("activity_support"):
                self.status[sid]["activity_support"] = previous["activity_support"]
            if health.get("ok"):
                await self._poll_activity(studio)
            self._note_transition(studio, prev_status, effective_status)
        except Exception as exc:
            prev = self.status.get(sid, {})
            if prev_status == "up" and self._has_active_lease(sid):
                self.status[sid] = {
                    **prev, "latency_ms": None, "last_checked": now,
                    "health_busy": True, "health_probe_degraded": False,
                }
                return
            failures = int(prev.get("consecutive_failures") or 0) + 1
            down = failures >= HEALTH_FAILURES_TO_DOWN
            effective_status = "down" if down else prev_status
            self.status[sid] = {
                **prev,
                "status": effective_status,
                "latency_ms": None,
                "app_version": prev.get("app_version"),
                "last_seen": prev.get("last_seen"),
                "last_checked": now,
                "consecutive_failures": failures,
                "consecutive_successes": 0,
                "health_busy": False,
                "health_recovering": False,
                "health_probe_degraded": not down,
                "probe_error": (str(exc).strip() or type(exc).__name__)[:180],
            }
            if down:
                self.status[sid]["health"] = None
            self._note_transition(studio, prev_status, effective_status)

    async def _poll_activity(self, studio: dict) -> None:
        """Fetch the optional private activity contract after health succeeds.

        Activity reporting is intentionally not a health dependency: a 404 is
        an older compatible Studio, while any other fault keeps the last good
        evidence for Stats and only marks its evidence limitation.
        """
        from . import activity

        sid = studio["id"]
        previous = self.status.get(sid, {})
        try:
            url, headers = studio_request(studio, "/api/fleet/activity")
            response = await self._client.get(
                url, headers=headers, timeout=HEALTH_TIMEOUT_S,
            )
            status_code = getattr(response, "status_code", 200)
            if status_code == 404:
                previous.pop("activity", None)
                previous["activity_support"] = "unavailable"
                previous.pop("activity_error", None)
                return
            if status_code >= 400:
                raise RuntimeError("activity reporter request failed")
            snapshot = activity.validate_snapshot(response.json())
            if snapshot is None:
                raise RuntimeError("activity reporter returned an invalid snapshot")
            received_at = time.time()
            if abs(snapshot["observed_at"] - received_at) > activity.REPORTER_CLOCK_SKEW_S:
                previous["activity_support"] = "skew"
                previous["activity_error"] = "reporter clock is outside policy"
                return
            previous["activity"] = snapshot
            previous["activity_received_at"] = received_at
            previous["activity_support"] = "available"
            previous.pop("activity_error", None)
        except Exception:
            previous["activity_support"] = "error"
            previous["activity_error"] = "reporter request failed"

    # ── catalog ──────────────────────────────────────────────────────────
    async def get_catalog(self, studio: dict, force: bool = False) -> dict | None:
        sid = studio["id"]
        cached = self._catalog_cache.get(sid)
        if cached and not force and (time.time() - cached[0]) < CATALOG_TTL_S:
            return cached[1]
        try:
            url, headers = studio_request(studio, "/api/catalog")
            r = await self._client.get(
                url, headers=headers,
                timeout=CATALOG_TIMEOUT_S
            )
            r.raise_for_status()
            data = r.json()
            self._catalog_cache[sid] = (time.time(), data)
            self._catalog_meta.setdefault(sid, {}).update(
                last_success_at=time.time(), last_error=None,
            )
            return data
        except Exception as exc:
            self._catalog_meta.setdefault(sid, {})["last_error"] = \
                self._safe_error(exc)
            # Serve stale on failure rather than nothing.
            return cached[1] if cached else None

    async def _catalog_for_aggregate(self, studio: dict, force: bool):
        """Fetch only from studios the poller says are UP. Down studios don't
        get a network call at all — we reuse their last-cached catalog (or
        contribute nothing). At fleet scale this keeps aggregation fast instead
        of waiting on dozens of offline studios' timeouts."""
        sid = studio["id"]
        if self.status.get(sid, {}).get("status") != "up":
            cached = self._catalog_cache.get(sid)
            return cached[1] if cached else None
        return await self.get_catalog(studio, force=force)

    async def get_transcription(self, studio: dict, force: bool = False) -> dict | None:
        """Voice Studio's Whisper inventory lives outside its TTS catalog."""
        sid = studio["id"]
        cached = self._transcribe_cache.get(sid)
        if cached and not force and (time.time() - cached[0]) < CATALOG_TTL_S:
            return cached[1]
        if self.status.get(sid, {}).get("status") != "up":
            return cached[1] if cached else None
        try:
            url, headers = studio_request(studio, "/api/transcribe/availability")
            r = await self._client.get(
                url, headers=headers, timeout=CATALOG_TIMEOUT_S,
            )
            r.raise_for_status()
            data = r.json()
            self._transcribe_cache[sid] = (time.time(), data)
            self._catalog_meta.setdefault(sid, {}).update(
                last_success_at=time.time(), last_error=None,
            )
            return data
        except Exception as exc:
            self._catalog_meta.setdefault(sid, {})["last_error"] = \
                self._safe_error(exc)
            return cached[1] if cached else None

    def _assemble_aggregate_catalog(
            self, results: list[dict | None],
            transcription: list[dict | None]) -> dict:
        """Assemble catalog and transcription observations already in memory.

        This helper is deliberately pure: it annotates supplied snapshots but
        never decides whether a cache should be refreshed and never performs a
        worker request. Callers choose either the live-refresh or cached-only
        path before reaching this point.
        """
        models, per_studio = [], {}
        for studio, catalog in zip(self.registry, results):
            sid = studio["id"]
            if catalog is None:
                per_studio[sid] = {"ok": False, "models": 0}
                continue
            from . import cloud_guard

            reported_entries = catalog.get("models") or []
            entries = [
                model for model in reported_entries
                if cloud_guard.cloud_reason(
                    model.get("repo"), modality=studio["modality"],
                    entries=(model,),
                ) is None
            ]
            for m in entries:
                annotated = dict(m)
                # Do not federate obsolete hosted-lane metadata. Render's raw
                # dispatch hint remains in its direct worker catalog, where the
                # broker reads it, but the Hub's public aggregate is local-only.
                for field in (
                    "is_cloud", "cloud_provider", "cloud_model_id",
                    "provider_model", "cloud_credentials_ok", "key_set",
                    "cost_tier", "price",
                ):
                    annotated.pop(field, None)
                annotated["hub_studio"] = sid
                annotated["hub_modality"] = studio["modality"]
                annotated["hub_machine"] = studio.get("machine", "local")
                annotated["hub_cached"] = is_cached(m)  # correct download state
                observation = self.catalog_observation(sid)
                annotated["hub_catalog_observed_at"] = observation["observed_at"]
                annotated["hub_catalog_age_seconds"] = observation["age_seconds"]
                annotated["hub_catalog_stale"] = observation["stale"]
                annotated["hub_catalog_error"] = observation["last_error"]
                models.append(annotated)
            per_studio[sid] = {
                "ok": True, "models": len(entries),
                "retired_cloud_models": len(reported_entries) - len(entries),
                "catalog": self.catalog_observation(sid),
            }

        # Whisper/STT is a separate Voice Studio subsystem and therefore is not
        # present in /api/catalog. Fold it into the fleet catalog as its own
        # modality so clients never need direct Voice Studio URLs for discovery.
        voice_studios = [s for s in self.registry if s.get("modality") == "voice"]
        for studio, availability in zip(voice_studios, transcription):
            if not availability:
                continue
            sid = studio["id"]
            ready = bool(availability.get("available"))
            default_repo = availability.get("default_model")
            entries = availability.get("models") or []
            for model in entries:
                annotated = dict(model)
                annotated["hub_studio"] = sid
                annotated["hub_modality"] = "transcription"
                annotated["hub_machine"] = studio.get("machine", "local")
                annotated["hub_cached"] = bool(model.get("cached"))
                annotated["hub_ready"] = ready
                annotated["default"] = model.get("repo") == default_repo
                observation = self.catalog_observation(sid, transcription=True)
                annotated["hub_catalog_observed_at"] = observation["observed_at"]
                annotated["hub_catalog_age_seconds"] = observation["age_seconds"]
                annotated["hub_catalog_stale"] = observation["stale"]
                annotated["hub_catalog_error"] = observation["last_error"]
                models.append(annotated)
            per_studio.setdefault(sid, {}).update({
                "transcription_ok": ready,
                "transcription_models": len(entries),
            })
        return {"models": models, "per_studio": per_studio, "total": len(models)}

    def cached_aggregate_catalog(self) -> dict:
        """Merge the last observed catalog and transcription caches only.

        Capability publication uses this path so a GenStudio read can never
        trigger worker HTTP calls or inherit worker timeouts. Expired cache
        entries remain useful as last-known capability evidence; worker health
        and readiness are reported separately by the capability contract.
        """
        results = [
            self._catalog_cache.get(studio["id"], (0.0, None))[1]
            for studio in self.registry
        ]
        voice_studios = [
            studio for studio in self.registry
            if studio.get("modality") == "voice"
        ]
        transcription = [
            self._transcribe_cache.get(studio["id"], (0.0, None))[1]
            for studio in voice_studios
        ]
        return self._assemble_aggregate_catalog(results, transcription)

    def cached_catalog_entries(
        self, model: str, *, modality: str | None = None,
    ) -> list[dict]:
        """Return last-good worker entries for one exact model without I/O.

        The broker uses this cache-only view to compare queued workloads before
        it selects a worker.  A scheduling decision must never turn into a live
        catalogue fan-out; the dedicated refresher remains the only owner of
        fresh fleet inventory.  The actual pre-dispatch catalogue and memory
        gates still run afterwards and remain authoritative.
        """
        matches: list[dict] = []
        for studio in self.registry:
            if modality is not None and studio.get("modality") != modality:
                continue
            cached = self._catalog_cache.get(studio["id"])
            catalog = cached[1] if cached else None
            for entry in (catalog or {}).get("models", []):
                if not isinstance(entry, dict):
                    continue
                if (entry.get("repo") == model
                        or model in (entry.get("aliases") or [])):
                    matches.append({"studio": studio, "entry": entry})
        return matches

    async def aggregate_catalog(self, force: bool = False) -> dict:
        """Merge all studios' catalogs. Models pass through verbatim, annotated
        with hub_studio / hub_modality / hub_machine so clients know the source."""
        results = await asyncio.gather(
            *(self._catalog_for_aggregate(s, force) for s in self.registry)
        )
        voice_studios = [s for s in self.registry if s.get("modality") == "voice"]
        transcription = await asyncio.gather(
            *(self.get_transcription(s, force=force) for s in voice_studios)
        )
        assembled = self._assemble_aggregate_catalog(results, transcription)
        if force:
            self._save_catalog_state()
        return assembled

    def candidate_models(self) -> list[dict]:
        """Cache-only audited candidate inventory grouped by exact contract.

        This is the operator review surface. It intentionally includes
        unexposed candidates; GenStudio's capability contract does not.
        """
        from . import (broker, chat_jobs, hardware_profiles, memory_admission,
                       model_exposure, transcription_jobs)
        from .registry import machine_enabled, studio_enabled

        aggregate = self.cached_aggregate_catalog()
        studios = {studio["id"]: studio for studio in self.registry}
        protections = broker.machine_protection_snapshot()
        busy_studios = set(broker.busy_studios()) | set(chat_jobs.busy_studios) \
            | set(transcription_jobs.busy_studios)
        busy_machines = broker.busy_machines()
        grouped: dict[str, dict] = {}

        for model in aggregate.get("models") or []:
            candidate = model_exposure.candidate_summary(model)
            if candidate is None:
                continue
            studio_id = str(model.get("hub_studio") or "")
            studio = studios.get(studio_id, {})
            machine = str(model.get("hub_machine") or "local")
            status = self.status.get(studio_id, {})
            online = status.get("status") == "up"
            quarantined = bool((protections.get(machine) or {}).get("quarantined"))
            busy = studio_id in busy_studios or machine in busy_machines
            drained = (
                not machine_enabled(machine)
                or not studio_enabled(machine, studio_id)
                or broker.in_maintenance(studio_id)
            )
            catalog_stale = bool(model.get("hub_catalog_stale"))
            installed = bool(model.get("hub_cached"))
            runtime_compatible = model.get("runtime_compatible") is not False
            subsystem_ready = model.get("hub_ready") is not False
            hardware_profile = hardware_profiles.machine_hardware_profile(machine)
            admission = None
            hardware_eligible = True
            if memory_admission.applies_to(model.get("hub_modality")):
                admission = memory_admission.describe(
                    candidate["internal_model_id"], model,
                )
                health_memory = ((status.get("health") or {}).get("memory") or {})
                observed_total = health_memory.get("total_gb")
                if not isinstance(observed_total, (int, float)):
                    observed_total = (hardware_profile or {}).get("memory_gb")
                floor = admission.get("effective_min_total_memory_gb")
                if isinstance(observed_total, (int, float)) and isinstance(floor, (int, float)):
                    hardware_eligible = float(observed_total) >= float(floor)
                admission = {
                    **admission,
                    "observed_total_memory_gb": observed_total,
                    "eligible": hardware_eligible,
                }

            if not online:
                reason = "worker_offline"
            elif quarantined:
                reason = "machine_quarantined"
            elif drained:
                reason = "worker_drained"
            elif catalog_stale:
                reason = "catalog_stale"
            elif not installed:
                reason = "model_not_installed"
            elif not runtime_compatible:
                reason = "runtime_incompatible"
            elif not subsystem_ready:
                reason = "subsystem_unavailable"
            elif not hardware_eligible:
                reason = "insufficient_total_memory"
            elif busy:
                reason = "machine_busy"
            else:
                reason = None
            ready = reason is None

            for operation in candidate["approved_operations"]:
                key = model_exposure.candidate_key(candidate, operation)
                row = grouped.setdefault(key, {
                    "candidate_key": key,
                    "internal_model_id": candidate["internal_model_id"],
                    "display_name": candidate["display_name"],
                    "modality": model.get("hub_modality"),
                    "operation": operation,
                    "runtime_revision": candidate["runtime_revision"],
                    "contract_hash": candidate["contract_hash"],
                    "audit_id": candidate.get("audit_id"),
                    "audit_status": candidate["audit_status"],
                    "candidate_for_genstudio": candidate["candidate_for_genstudio"],
                    "audited_at": candidate.get("audited_at"),
                    "adapter": candidate.get("adapter", {}),
                    "controls": candidate.get("controls", {}),
                    "input_limits": candidate.get("input_limits", {}),
                    "output_limits": candidate.get("output_limits", {}),
                    "capacity": candidate.get("capacity", {}),
                    "hardware": candidate.get("hardware", {}),
                    "exposure": model_exposure.state_for(candidate, operation),
                    "machines": [],
                })
                row["machines"].append({
                    "studio_id": studio_id,
                    "machine_id": machine,
                    "installed": installed,
                    "online": online,
                    "ready": ready,
                    "busy": busy,
                    "quarantined": quarantined,
                    "drained": drained,
                    "hardware_profile": hardware_profile,
                    "memory_admission": admission,
                    "availability_reason": reason,
                    "catalog_observed_at": model.get("hub_catalog_observed_at"),
                    "catalog_age_seconds": model.get("hub_catalog_age_seconds"),
                    "catalog_stale": catalog_stale,
                    "reported_available_slots": candidate.get(
                        "capacity", {},
                    ).get("available_slots", 1),
                })

        rows = list(grouped.values())
        variants: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for row in rows:
            variants.setdefault(
                (row["internal_model_id"], row["operation"]), set(),
            ).add((row["runtime_revision"], row["contract_hash"]))
        for row in rows:
            machines = row["machines"]
            row["contract_conflict"] = len(variants[
                (row["internal_model_id"], row["operation"])
            ]) > 1
            row["supply"] = {
                "installed_machine_count": len({m["machine_id"] for m in machines
                                                if m["installed"]}),
                "online_machine_count": len({m["machine_id"] for m in machines
                                             if m["online"]}),
                "ready_machine_count": len({m["machine_id"] for m in machines
                                            if m["ready"]}),
                "busy_machine_count": len({m["machine_id"] for m in machines
                                           if m["busy"]}),
                "offline_machine_count": len({m["machine_id"] for m in machines
                                               if not m["online"]}),
                "quarantined_machine_count": len({
                    m["machine_id"] for m in machines if m["quarantined"]
                }),
                "offline_or_quarantined_machine_count": len({
                    m["machine_id"] for m in machines
                    if not m["online"] or m["quarantined"]
                }),
                # Hub schedules one heavy job per physical Mac even when a
                # sibling reports greater service-level concurrency.
                "available_physical_slots": len({
                    m["machine_id"] for m in machines if m["ready"]
                    and m["reported_available_slots"] > 0
                }),
            }
        return sorted(rows, key=lambda row: (
            row["modality"] or "", row["display_name"], row["operation"],
            row["runtime_revision"],
        ))

    async def models_by_repo(self, force: bool = False) -> list[dict]:
        """Deduped by repo across all machines, with per-machine availability.
        This is what the Models tab needs: one row per model, showing WHICH
        machines have it downloaded (so 'downloaded on the media server but not
        this Mac' reads correctly instead of a blanket 'downloaded')."""
        agg = (
            await self.aggregate_catalog(force=True)
            if force else self.cached_aggregate_catalog()
        )
        by_repo: dict[str, dict] = {}
        for m in agg["models"]:
            repo = m.get("repo")
            if not repo:
                continue
            row = by_repo.get(repo)
            if row is None:
                row = by_repo[repo] = {
                    "repo": repo,
                    "label": m.get("label") or repo,
                    "modality": m.get("hub_modality"),
                    "family_label": m.get("family_label") or m.get("family"),
                    "size_gb": m.get("size_gb"),
                    "min_unified_memory_gb": m.get("min_unified_memory_gb"),
                    "recommended": bool(m.get("recommended") or m.get("default")),
                    "note": m.get("note"),
                    "machines": [],       # every studio that lists it
                    "cached_on": [],      # machine names where it's downloaded
                    "available_on": [],   # machines whose runtime is ready
                    "catalog_observations": [],
                    "genstudio_candidates": [],
                }
            machine = m.get("hub_machine", "local")
            row["machines"].append({"studio": m.get("hub_studio"),
                                    "machine": machine, "cached": m.get("hub_cached"),
                                    "ready": m.get("hub_ready"),
                                    "catalog_stale": m.get("hub_catalog_stale"),
                                    "catalog_age_seconds": m.get("hub_catalog_age_seconds")})
            row["catalog_observations"].append({
                "studio": m.get("hub_studio"), "machine": machine,
                "observed_at": m.get("hub_catalog_observed_at"),
                "age_seconds": m.get("hub_catalog_age_seconds"),
                "stale": m.get("hub_catalog_stale"),
                "error": m.get("hub_catalog_error"),
            })
            from . import model_exposure
            candidate = model_exposure.candidate_summary(m)
            if candidate:
                for operation in candidate["approved_operations"]:
                    row["genstudio_candidates"].append({
                        **candidate,
                        "operation": operation,
                        "exposure": model_exposure.state_for(candidate, operation),
                    })
            if m.get("hub_cached") and machine not in row["cached_on"]:
                row["cached_on"].append(machine)
            if m.get("hub_ready") and m.get("hub_cached") and machine not in row["available_on"]:
                row["available_on"].append(machine)
        rows = list(by_repo.values())
        for r in rows:
            r["downloaded"] = bool(r["cached_on"]) or r["modality"] == "render"
            r["available"] = bool(r["available_on"]) if r["modality"] == "transcription" else True
        rows.sort(key=lambda r: (
            r["modality"] or "",
            not r["downloaded"], r["repo"],
        ))
        return rows

    async def transcription_inventory(self, force: bool = False) -> dict:
        rows = [r for r in await self.models_by_repo(force=force)
                if r.get("modality") == "transcription"]
        machines = {m["machine"] for row in rows for m in row.get("machines", [])}
        ready = {m for row in rows for m in row.get("available_on", [])}
        default = next((r["repo"] for r in rows
                        if r.get("recommended") and r.get("downloaded")), None)
        if default is None:
            default = next((r["repo"] for r in rows if r.get("downloaded")), None)
        return {
            "available": bool(ready),
            "models": [{
                "repo": r["repo"], "label": r["label"], "size_gb": r.get("size_gb"),
                "note": r.get("note"), "recommended": r.get("recommended", False),
                "cached": r.get("downloaded", False), "cached_on": r.get("cached_on", []),
                "available_on": r.get("available_on", []), "machines": r.get("machines", []),
            } for r in rows],
            "default_model": default,
            "endpoint_count": len(machines),
            "ready_count": len(ready),
        }
