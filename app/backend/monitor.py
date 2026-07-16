"""Health poller + catalog aggregator.

Polls every studio's /api/health on a short interval and caches /api/catalog
with a TTL. Model params/schemas are never normalized (SPEC §6.2) — catalog
entries are passed through verbatim, only ANNOTATED with hub_* fields naming
the source studio.
"""

import asyncio
import logging
import time

import httpx

log = logging.getLogger("studiohub.monitor")

from .registry import base_url, load_registry, prune_machine_metadata
from .peers import studio_request

POLL_INTERVAL_S = 5.0
HEALTH_TIMEOUT_S = 3.0
CATALOG_TIMEOUT_S = 10.0
CATALOG_TTL_S = 60.0
PROVIDER_TIMEOUT_S = 3.0
PROVIDER_TTL_S = 30.0


def is_cached(model: dict) -> bool:
    """Whether a studio has this model fully downloaded.

    Every studio reports `cache` as a dict {state: 'cached'|'absent'|'partial'}.
    The trap: bool(cache) is True for ANY non-empty dict, so a naive truthiness
    check marks even 'absent' models as downloaded. Only 'cached' counts."""
    cache = model.get("cache")
    if isinstance(cache, dict):
        return cache.get("state") == "cached"
    return bool(cache)  # tolerate a studio that ever uses a bool/string


# 'render' is a local FFmpeg episode-assembly step. Render studios flag their
# catalog entry is_cloud=true ONLY to bypass the broker's download/memory gates
# (a dispatch hint, not a hosting statement), so it must never land in the cloud
# lane or count as a cloud generation. Any other is_cloud entry is a genuine
# external-provider (cloud) model.
LOCAL_ONLY_MODALITIES = {"render"}


def is_cloud_lane(is_cloud, modality) -> bool:
    """Whether an entry belongs in the CLOUD lane (external provider) for the
    dashboard and ledger — distinct from the broker's raw is_cloud dispatch flag,
    which render overloads as a governor bypass."""
    return bool(is_cloud) and modality not in LOCAL_ONLY_MODALITIES


def _provider_of(model: dict) -> str:
    """The effective cloud provider used to GROUP a cloud model in the Models
    tab (fal / cloudflare / gemini / …).

    Prefer the studio-supplied `provider`, but existing Image/Chat cloud entries
    set a generic literal ``"cloud"`` and encode the real vendor in the repo
    prefix (``cloudflare/flux-1-schnell`` → ``cloudflare``). Fall back to that
    prefix so those group correctly today, while Video's explicit
    ``provider="fal"`` is used verbatim. Last resort: the literal ``"cloud"``."""
    p = (model.get("provider") or "").strip()
    if p and p.lower() != "cloud":
        return p
    repo = model.get("repo") or ""
    if "/" in repo:
        prefix = repo.split("/", 1)[0].strip()
        if prefix:
            return prefix
    return p or "cloud"


class StudioMonitor:
    def __init__(self):
        self.registry: list[dict] = load_registry()
        prune_machine_metadata({studio.get("machine", "local")
                                for studio in self.registry})
        self.status: dict[str, dict] = {
            s["id"]: {"status": "unknown", "last_seen": None, "last_checked": None}
            for s in self.registry
        }
        self._catalog_cache: dict[str, tuple[float, dict]] = {}
        self._transcribe_cache: dict[str, tuple[float, dict]] = {}
        self._provider_cache: dict[str, tuple[float, dict]] = {}
        self._client = httpx.AsyncClient()
        self._task: asyncio.Task | None = None

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self):
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
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
            self._provider_cache.pop(studio_id, None)

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
        # Each peer Hub is authoritative for the providers on its own Voice
        # Studio. Remote provider health arrives through the peer resource
        # snapshot, so this Hub only calls its local Voice Studio directly.
        await asyncio.gather(*(
            self.get_provider_health(s)
            for s in self.registry
            if s.get("modality") == "voice"
            and s.get("machine", "local") == "local"
            and self.status.get(s["id"], {}).get("status") == "up"
        ))
        # metrics sample + watchdog revival pass (late import: no cycle)
        from . import metrics, peers
        metrics.on_poll(self.registry, self.status)
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
            new_status = "up" if health.get("ok") else "degraded"
            self.status[sid] = {
                "status": new_status,
                "latency_ms": latency_ms,
                "app_version": health.get("app_version"),
                "health": health,  # verbatim — includes chat's loaded_model etc.
                "last_seen": now,
                "last_checked": now,
            }
            self._note_transition(studio, prev_status, new_status)
        except Exception:
            prev = self.status.get(sid, {})
            if prev_status == "up" and self._has_active_lease(sid):
                self.status[sid] = {
                    **prev, "latency_ms": None, "last_checked": now,
                    "health_busy": True,
                }
                return
            self.status[sid] = {
                "status": "down",
                "latency_ms": None,
                "app_version": prev.get("app_version"),
                "health": None,
                "last_seen": prev.get("last_seen"),
                "last_checked": now,
            }
            self._note_transition(studio, prev_status, "down")

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
            return data
        except Exception:
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
            return data
        except Exception:
            return cached[1] if cached else None

    async def get_provider_health(self, studio: dict, force: bool = False) -> dict:
        """Cache Voice Studio's public cloud-provider readiness summary.

        Only explicit public fields are retained. API keys and future private
        settings can never leak into peer snapshots even if Voice Studio adds
        them to its provider response later.
        """
        sid = studio["id"]
        cached = self._provider_cache.get(sid)
        if cached and not force and (time.time() - cached[0]) < PROVIDER_TTL_S:
            return cached[1]
        if self.status.get(sid, {}).get("status") != "up":
            return cached[1] if cached else {"supported": False, "providers": []}
        try:
            url, headers = studio_request(studio, "/api/providers")
            r = await self._client.get(
                url, headers=headers, timeout=PROVIDER_TIMEOUT_S,
            )
            if r.status_code == 404:
                data = {"supported": False, "providers": [], "stale": False}
            else:
                r.raise_for_status()
                rows = r.json().get("providers") or []
                data = {
                    "supported": True,
                    "stale": False,
                    "providers": [{
                        "key": str(row.get("key") or ""),
                        "name": str(row.get("name") or row.get("key") or ""),
                        "has_key": bool(row.get("has_key")),
                        "paid": bool(row.get("paid")),
                        "enabled": bool(row.get("enabled")),
                        "live": bool(row.get("live")),
                        "models": len(row.get("models") or []),
                    } for row in rows if row.get("key")],
                }
            self._provider_cache[sid] = (time.time(), data)
            return data
        except Exception:
            data = {
                **(cached[1] if cached else {"supported": False, "providers": []}),
                "stale": True,
            }
            self._provider_cache[sid] = (time.time(), data)
            return data

    def provider_health(self, studio_id: str) -> dict:
        cached = self._provider_cache.get(studio_id)
        return cached[1] if cached else {"supported": False, "providers": []}

    async def aggregate_catalog(self, force: bool = False) -> dict:
        """Merge all studios' catalogs. Models pass through verbatim, annotated
        with hub_studio / hub_modality / hub_machine so clients know the source."""
        results = await asyncio.gather(
            *(self._catalog_for_aggregate(s, force) for s in self.registry)
        )
        models, per_studio = [], {}
        for studio, catalog in zip(self.registry, results):
            sid = studio["id"]
            if catalog is None:
                per_studio[sid] = {"ok": False, "models": 0}
                continue
            entries = catalog.get("models") or []
            for m in entries:
                annotated = dict(m)
                annotated["hub_studio"] = sid
                annotated["hub_modality"] = studio["modality"]
                annotated["hub_machine"] = studio.get("machine", "local")
                annotated["hub_cached"] = is_cached(m)  # correct download state
                models.append(annotated)
            per_studio[sid] = {"ok": True, "models": len(entries)}

        # Whisper/STT is a separate Voice Studio subsystem and therefore is not
        # present in /api/catalog. Fold it into the fleet catalog as its own
        # modality so clients never need direct Voice Studio URLs for discovery.
        voice_studios = [s for s in self.registry if s.get("modality") == "voice"]
        transcription = await asyncio.gather(
            *(self.get_transcription(s, force=force) for s in voice_studios)
        )
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
                models.append(annotated)
            per_studio.setdefault(sid, {}).update({
                "transcription_ok": ready,
                "transcription_models": len(entries),
            })
        return {"models": models, "per_studio": per_studio, "total": len(models)}

    async def models_by_repo(self, force: bool = False) -> list[dict]:
        """Deduped by repo across all machines, with per-machine availability.
        This is what the Models tab needs: one row per model, showing WHICH
        machines have it downloaded (so 'downloaded on the media server but not
        this Mac' reads correctly instead of a blanket 'downloaded')."""
        agg = await self.aggregate_catalog(force=force)
        by_repo: dict[str, dict] = {}
        for m in agg["models"]:
            repo = m.get("repo")
            if not repo:
                continue
            row = by_repo.get(repo)
            if row is None:
                # cloud LANE classification — not the raw is_cloud dispatch flag
                # (render sets is_cloud=true purely as a broker governor bypass).
                is_cloud = is_cloud_lane(m.get("is_cloud"), m.get("hub_modality"))
                row = by_repo[repo] = {
                    "repo": repo,
                    "label": m.get("label") or repo,
                    "modality": m.get("hub_modality"),
                    "family_label": m.get("family_label") or m.get("family"),
                    "size_gb": m.get("size_gb"),
                    "min_unified_memory_gb": m.get("min_unified_memory_gb"),
                    "is_cloud": is_cloud,
                    "lane": "cloud" if is_cloud else "local",
                    # cloud metadata (present only on cloud entries; harmless None
                    # for local): provider (fal/kie/…), tier, availability status,
                    # and the price object, so the UI can badge/group them.
                    "provider": _provider_of(m) if is_cloud else None,
                    "cost_tier": m.get("cost_tier"),
                    "status": m.get("status"),
                    "price": m.get("price"),
                    "recommended": bool(m.get("recommended") or m.get("default")),
                    "note": m.get("note"),
                    "machines": [],       # every studio that lists it
                    "cached_on": [],      # machine names where it's downloaded
                    "available_on": [],   # machines whose runtime is ready
                }
            machine = m.get("hub_machine", "local")
            row["machines"].append({"studio": m.get("hub_studio"),
                                    "machine": machine, "cached": m.get("hub_cached"),
                                    "ready": m.get("hub_ready")})
            if m.get("hub_cached") and machine not in row["cached_on"]:
                row["cached_on"].append(machine)
            if m.get("hub_ready") and m.get("hub_cached") and machine not in row["available_on"]:
                row["available_on"].append(machine)
        rows = list(by_repo.values())
        for r in rows:
            r["downloaded"] = bool(r["cached_on"]) or r["is_cloud"]
            r["available"] = bool(r["available_on"]) if r["modality"] == "transcription" else True
        rows.sort(key=lambda r: (
            0 if r["lane"] == "local" else 1,                              # local lane first
            (r.get("provider") or "") if r["lane"] == "cloud" else "",     # then provider (cloud)
            r["modality"] or "",                                            # then modality within
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
