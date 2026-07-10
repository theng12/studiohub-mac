"""Health poller + catalog aggregator.

Polls every studio's /api/health on a short interval and caches /api/catalog
with a TTL. Model params/schemas are never normalized (SPEC §6.2) — catalog
entries are passed through verbatim, only ANNOTATED with hub_* fields naming
the source studio.
"""

import asyncio
import time

import httpx

from .registry import base_url, load_registry

POLL_INTERVAL_S = 5.0
HEALTH_TIMEOUT_S = 3.0
CATALOG_TIMEOUT_S = 10.0
CATALOG_TTL_S = 60.0


def is_cached(model: dict) -> bool:
    """Whether a studio has this model fully downloaded.

    Every studio reports `cache` as a dict {state: 'cached'|'absent'|'partial'}.
    The trap: bool(cache) is True for ANY non-empty dict, so a naive truthiness
    check marks even 'absent' models as downloaded. Only 'cached' counts."""
    cache = model.get("cache")
    if isinstance(cache, dict):
        return cache.get("state") == "cached"
    return bool(cache)  # tolerate a studio that ever uses a bool/string


class StudioMonitor:
    def __init__(self):
        self.registry: list[dict] = load_registry()
        self.status: dict[str, dict] = {
            s["id"]: {"status": "unknown", "last_seen": None, "last_checked": None}
            for s in self.registry
        }
        self._catalog_cache: dict[str, tuple[float, dict]] = {}
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
        for s in self.registry:
            self.status.setdefault(
                s["id"],
                {"status": "unknown", "last_seen": None, "last_checked": None},
            )

    # ── health ───────────────────────────────────────────────────────────
    async def _poll_loop(self):
        while True:
            try:
                await self.poll_all()
            except Exception:
                pass  # the poller must never die
            await asyncio.sleep(POLL_INTERVAL_S)

    async def poll_all(self):
        await asyncio.gather(*(self._poll_one(s) for s in self.registry))
        # metrics sample + watchdog revival pass (late import: no cycle)
        from . import metrics, peers
        metrics.on_poll(self.registry, self.status)
        # refresh peer-Hub resources in the background (TTL-guarded + in-flight
        # guarded inside) so a slow/offline fleet never stalls the health poll.
        asyncio.create_task(peers.refresh(self.registry, self._client))

    async def _poll_one(self, studio: dict):
        sid = studio["id"]
        url = f"{base_url(studio)}/api/health"
        started = time.monotonic()
        now = time.time()
        try:
            r = await self._client.get(url, timeout=HEALTH_TIMEOUT_S)
            latency_ms = round((time.monotonic() - started) * 1000)
            health = r.json()
            self.status[sid] = {
                "status": "up" if health.get("ok") else "degraded",
                "latency_ms": latency_ms,
                "app_version": health.get("app_version"),
                "health": health,  # verbatim — includes chat's loaded_model etc.
                "last_seen": now,
                "last_checked": now,
            }
        except Exception:
            prev = self.status.get(sid, {})
            self.status[sid] = {
                "status": "down",
                "latency_ms": None,
                "app_version": prev.get("app_version"),
                "health": None,
                "last_seen": prev.get("last_seen"),
                "last_checked": now,
            }

    # ── catalog ──────────────────────────────────────────────────────────
    async def get_catalog(self, studio: dict, force: bool = False) -> dict | None:
        sid = studio["id"]
        cached = self._catalog_cache.get(sid)
        if cached and not force and (time.time() - cached[0]) < CATALOG_TTL_S:
            return cached[1]
        try:
            r = await self._client.get(
                f"{base_url(studio)}/api/catalog", timeout=CATALOG_TIMEOUT_S
            )
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
                row = by_repo[repo] = {
                    "repo": repo,
                    "label": m.get("label") or repo,
                    "modality": m.get("hub_modality"),
                    "family_label": m.get("family_label") or m.get("family"),
                    "size_gb": m.get("size_gb"),
                    "min_unified_memory_gb": m.get("min_unified_memory_gb"),
                    "is_cloud": bool(m.get("is_cloud")),
                    "machines": [],       # every studio that lists it
                    "cached_on": [],      # machine names where it's downloaded
                }
            machine = m.get("hub_machine", "local")
            row["machines"].append({"studio": m.get("hub_studio"),
                                    "machine": machine, "cached": m.get("hub_cached")})
            if m.get("hub_cached") and machine not in row["cached_on"]:
                row["cached_on"].append(machine)
        rows = list(by_repo.values())
        for r in rows:
            r["downloaded"] = bool(r["cached_on"]) or r["is_cloud"]
        rows.sort(key=lambda r: (r["modality"] or "", not r["downloaded"], r["repo"]))
        return rows
