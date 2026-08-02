"""GenStudio-approved model desired state enforced by one site controller.

GenStudio approves exact audited contracts once.  Every controller persists
the last-good desired-state document and asks eligible sibling Studios to cache
those models.  Download execution remains sibling-owned and resumable; removing
a model from desired state never deletes cached or partial files.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from . import hardware_profiles, model_exposure, peers


CATALOG_SCHEMA = "genstudio.fleet-model-catalog"
CATALOG_SCHEMA_VERSION = 1
DEFAULT_RECONCILE_SECONDS = 15 * 60
_HASH = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^(?:sha256:)?[0-9a-f]{40,64}$")
_CONTRACT = re.compile(r"^sha256:[0-9a-f]{64}$")


class FleetModelBaselines:
    """Compatibility name for the dynamic approved fleet catalog reconciler."""

    def __init__(
        self,
        monitor,
        *,
        state_path: Path,
        reconcile_seconds: float = DEFAULT_RECONCILE_SECONDS,
    ):
        self.monitor = monitor
        self.state_path = state_path
        self.reconcile_seconds = max(60.0, float(reconcile_seconds))
        self.enabled = True
        self.catalog_revision: str | None = None
        self.catalog_generated_at: str | None = None
        self.catalog_synced_at: float | None = None
        self.models: list[dict[str, Any]] = []
        self.last_reconciled_at: float | None = None
        self.targets: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._triggered_task: asyncio.Task | None = None
        self._lock: asyncio.Lock | None = None
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        self.enabled = bool(payload.get("enabled", True))
        revision = payload.get("catalog_revision")
        self.catalog_revision = revision if isinstance(revision, str) else None
        generated = payload.get("catalog_generated_at")
        self.catalog_generated_at = generated if isinstance(generated, str) else None
        synced = payload.get("catalog_synced_at")
        self.catalog_synced_at = float(synced) if isinstance(synced, (int, float)) else None
        reconciled = payload.get("last_reconciled_at")
        self.last_reconciled_at = (
            float(reconciled) if isinstance(reconciled, (int, float)) else None
        )
        models = payload.get("models")
        if isinstance(models, list):
            self.models = [dict(row) for row in models if isinstance(row, dict)]
        targets = payload.get("targets")
        if isinstance(targets, dict):
            self.targets = {
                str(key): dict(value)
                for key, value in targets.items()
                if isinstance(value, dict)
            }

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "enabled": self.enabled,
                        "catalog_revision": self.catalog_revision,
                        "catalog_generated_at": self.catalog_generated_at,
                        "catalog_synced_at": self.catalog_synced_at,
                        "models": self.models,
                        "last_reconciled_at": self.last_reconciled_at,
                        "targets": self.targets,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.state_path)
        except OSError:
            pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for task in (self._task, self._triggered_task):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        self._triggered_task = None

    async def _loop(self) -> None:
        try:
            await asyncio.sleep(30)
            while True:
                if self.enabled and self.models:
                    with contextlib.suppress(Exception):
                        await self.reconcile()
                await asyncio.sleep(self.reconcile_seconds)
        except asyncio.CancelledError:
            pass

    def save_settings(self, *, enabled: bool) -> dict[str, Any]:
        self.enabled = bool(enabled)
        self._save()
        return self.snapshot()

    def trigger_reconcile(self) -> bool:
        """Schedule one non-overlapping reconciliation after desired-state sync."""
        if not self.enabled or not self.models:
            return False
        if self._triggered_task is not None and not self._triggered_task.done():
            return False
        self._triggered_task = asyncio.create_task(self.reconcile())
        return True

    @staticmethod
    def _validate_model(raw: object) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("Every fleet catalog model must be an object.")
        deployment = raw.get("deployment")
        if not isinstance(deployment, dict) or deployment.get("mode") != "all_eligible":
            raise ValueError("Fleet catalog models must use all_eligible deployment.")
        minimum = deployment.get("minimum_unified_memory_gb")
        if (
            not isinstance(minimum, (int, float))
            or isinstance(minimum, bool)
            or not 4 <= float(minimum) <= 512
        ):
            raise ValueError("Fleet catalog model memory eligibility is invalid.")
        model = {
            "candidate_key": str(raw.get("candidate_key") or "").lower(),
            "repo": str(raw.get("internal_model_id") or "").strip(),
            "label": str(raw.get("display_name") or "").strip()[:256],
            "modality": str(raw.get("modality") or "").strip().lower(),
            "operation": str(raw.get("operation") or "").strip(),
            "runtime_revision": str(raw.get("runtime_revision") or "").lower(),
            "contract_hash": str(raw.get("contract_hash") or "").lower(),
            "sibling_studio": str(raw.get("sibling_studio") or "").strip().lower(),
            "inventory": str(raw.get("inventory") or "catalog").strip().lower(),
            "min_unified_memory_gb": float(minimum),
            "deployment_policy": "all_eligible",
        }
        expected_key = model_exposure.exposure_key(
            model["repo"],
            model["operation"],
            model["runtime_revision"],
            model["contract_hash"],
        )
        if not model["repo"] or not model["label"]:
            raise ValueError("Fleet catalog model identity is incomplete.")
        if not _HASH.fullmatch(model["candidate_key"]) or model["candidate_key"] != expected_key:
            raise ValueError("Fleet catalog exact model key is invalid.")
        if not _REVISION.fullmatch(model["runtime_revision"]):
            raise ValueError("Fleet catalog runtime revision is not immutable.")
        if not _CONTRACT.fullmatch(model["contract_hash"]):
            raise ValueError("Fleet catalog contract hash is invalid.")
        if model["sibling_studio"] not in {"image", "voice", "video", "music", "chat"}:
            raise ValueError("Fleet catalog sibling Studio is unsupported.")
        if model["inventory"] not in {"catalog", "transcription"}:
            raise ValueError("Fleet catalog inventory source is unsupported.")
        return model

    def replace_catalog(self, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        if payload.get("schema") != CATALOG_SCHEMA:
            raise ValueError("Unsupported GenStudio fleet catalog schema.")
        if payload.get("schema_version") != CATALOG_SCHEMA_VERSION:
            raise ValueError("Unsupported GenStudio fleet catalog version.")
        if payload.get("authority") != "genstudio":
            raise ValueError("Fleet catalog authority must be GenStudio.")
        revision = str(payload.get("revision") or "").lower()
        if not _HASH.fullmatch(revision):
            raise ValueError("Fleet catalog revision is invalid.")
        raw_models = payload.get("models")
        if not isinstance(raw_models, list) or len(raw_models) > 200:
            raise ValueError("Fleet catalog models must be a bounded list.")
        models = [self._validate_model(row) for row in raw_models]
        if len({row["candidate_key"] for row in models}) != len(models):
            raise ValueError("Fleet catalog contains a duplicate exact model contract.")
        changed = revision != self.catalog_revision or models != self.models
        self.catalog_revision = revision
        generated = payload.get("generated_at")
        self.catalog_generated_at = generated if isinstance(generated, str) else None
        self.catalog_synced_at = time.time()
        self.models = models
        desired_repos = {row["repo"] for row in models}
        self.targets = {
            key: value
            for key, value in self.targets.items()
            if key.split("::", 1)[-1] in desired_repos
        }
        model_exposure.sync_global_catalog(models, revision=revision)
        self._save()
        return changed, self.snapshot()

    @staticmethod
    def _target_key(studio_id: str, repo: str) -> str:
        return f"{studio_id}::{repo}"

    def _machine_memory(self, studio: dict[str, Any]) -> tuple[float | None, str]:
        status = self.monitor.status.get(studio.get("id"), {})
        health_memory = ((status.get("health") or {}).get("memory") or {})
        observed = health_memory.get("total_gb")
        if isinstance(observed, (int, float)) and observed > 0:
            return float(observed), "live"
        profile = hardware_profiles.machine_hardware_profile(studio.get("machine", "local"))
        configured = (profile or {}).get("memory_gb")
        if isinstance(configured, (int, float)) and configured > 0:
            return float(configured), "profile"
        return None, "unknown"

    def _eligibility(self, studio: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
        actual, source = self._machine_memory(studio)
        required = float(model["min_unified_memory_gb"])
        return {
            "eligible": actual is not None and actual >= required,
            "required_memory_gb": required,
            "observed_memory_gb": actual,
            "memory_source": source,
        }

    def _target_models(self, studio: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            model
            for model in self.models
            if model["sibling_studio"] == studio.get("modality")
        ]

    def snapshot(self) -> dict[str, Any]:
        rows = []
        for studio in self.monitor.registry:
            for model in self._target_models(studio):
                key = self._target_key(studio["id"], model["repo"])
                row = dict(self.targets.get(key) or {})
                row.update(
                    {
                        "studio_id": studio["id"],
                        "machine": studio.get("machine", "local"),
                        "reachable": self.monitor.status.get(studio["id"], {}).get("status")
                        == "up",
                        "candidate_key": model["candidate_key"],
                        "model_repo": model["repo"],
                        "model_label": model["label"],
                        "operation": model["operation"],
                    }
                )
                row.update(self._eligibility(studio, model))
                if row["memory_source"] == "unknown":
                    row["state"] = "eligibility_unknown"
                    row["detail"] = "Machine memory is unknown; automatic caching is paused"
                elif not row["eligible"]:
                    row["state"] = "ineligible"
                    row["detail"] = (
                        f"Requires {model['min_unified_memory_gb']:g} GB unified memory; "
                        f"machine reports {row['observed_memory_gb']:g} GB"
                    )
                row.setdefault("state", "unknown")
                rows.append(row)
        return {
            "schema_version": 3,
            "schema": CATALOG_SCHEMA,
            "enabled": self.enabled,
            "authority": "genstudio" if self.catalog_revision else "awaiting_genstudio",
            "catalog_revision": self.catalog_revision,
            "catalog_generated_at": self.catalog_generated_at,
            "catalog_synced_at": self.catalog_synced_at,
            "scope": "every eligible registered sibling Studio worker",
            "models": self.models,
            "last_reconciled_at": self.last_reconciled_at,
            "reconcile_seconds": self.reconcile_seconds,
            "targets": rows,
            "summary": {
                "approved_models": len(self.models),
                "total": len(rows),
                "cached": sum(row.get("state") == "cached" for row in rows),
                "eligible_total": sum(bool(row.get("eligible")) for row in rows),
                "ineligible": sum(row.get("state") == "ineligible" for row in rows),
                "eligibility_unknown": sum(
                    row.get("state") == "eligibility_unknown" for row in rows
                ),
                "pending": sum(
                    row.get("state") in {"unknown", "offline", "queued", "running"}
                    and row.get("eligible")
                    for row in rows
                ),
                "failed": sum(row.get("state") in {"error", "audit_mismatch"} for row in rows),
            },
        }

    async def reconcile(self) -> dict[str, Any]:
        if self._lock is None:
            self._lock = asyncio.Lock()
        if self._lock.locked():
            return self.snapshot()
        async with self._lock:
            if self.enabled:
                for studio in self.monitor.registry:
                    if self._target_models(studio):
                        await self._reconcile_one(studio)
            self.last_reconciled_at = time.time()
            self._save()
            return self.snapshot()

    @staticmethod
    def _candidate_matches(model: dict[str, Any], required: dict[str, Any]) -> bool:
        candidate = model_exposure.candidate_summary(model)
        return bool(
            candidate
            and model_exposure.candidate_key(candidate, required["operation"])
            == required["candidate_key"]
            and candidate.get("audit_status") == "passed"
            and candidate.get("candidate_for_genstudio") is True
            and required["operation"] in candidate.get("approved_operations", [])
        )

    async def _reconcile_one(self, studio: dict[str, Any]) -> None:
        studio_id = studio["id"]
        required_models = self._target_models(studio)
        status = self.monitor.status.get(studio_id, {}).get("status")
        if status != "up":
            for model in required_models:
                eligibility = self._eligibility(studio, model)
                self.targets[self._target_key(studio_id, model["repo"])] = {
                    **eligibility,
                    "state": (
                        "eligibility_unknown"
                        if eligibility["memory_source"] == "unknown"
                        else "ineligible"
                        if not eligibility["eligible"]
                        else "offline"
                    ),
                    "detail": "Sibling Studio is unreachable; retrying automatically",
                    "checked_at": time.time(),
                }
            return

        needs_transcription = any(row["inventory"] == "transcription" for row in required_models)
        needs_catalog = any(row["inventory"] == "catalog" for row in required_models)
        try:
            transcription, catalog = await asyncio.gather(
                self.monitor.get_transcription(studio, force=True)
                if needs_transcription
                else asyncio.sleep(0, result={"models": []}),
                self.monitor.get_catalog(studio, force=True)
                if needs_catalog
                else asyncio.sleep(0, result={"models": []}),
            )
        except (httpx.HTTPError, ValueError, TypeError, OSError) as exc:
            for model in required_models:
                self.targets[self._target_key(studio_id, model["repo"])] = {
                    "state": "error",
                    "detail": (str(exc).strip() or type(exc).__name__)[:220],
                    "checked_at": time.time(),
                }
            return

        inventories = {
            "transcription": (transcription or {}).get("models", []),
            "catalog": (catalog or {}).get("models", []),
        }
        for required in required_models:
            key = self._target_key(studio_id, required["repo"])
            eligibility = self._eligibility(studio, required)
            if eligibility["memory_source"] == "unknown" or not eligibility["eligible"]:
                self.targets[key] = {
                    **eligibility,
                    "state": (
                        "eligibility_unknown"
                        if eligibility["memory_source"] == "unknown"
                        else "ineligible"
                    ),
                    "detail": "Automatic caching paused by hardware eligibility",
                    "checked_at": time.time(),
                }
                continue
            model = next(
                (
                    row
                    for row in inventories[required["inventory"]]
                    if isinstance(row, dict) and row.get("repo") == required["repo"]
                ),
                None,
            )
            if not isinstance(model, dict) or not self._candidate_matches(model, required):
                self.targets[key] = {
                    **eligibility,
                    "state": "audit_mismatch",
                    "detail": "Worker does not report the exact approved audit contract",
                    "checked_at": time.time(),
                }
                continue
            cached = (
                bool(model.get("cached"))
                if required["inventory"] == "transcription"
                else isinstance(model.get("cache"), dict)
                and model["cache"].get("state") == "cached"
            )
            if cached:
                self.targets[key] = {
                    **eligibility,
                    "state": "cached",
                    "detail": f"{required['label']} is ready",
                    "checked_at": time.time(),
                }
                continue
            try:
                url, headers = peers.studio_request(studio, "/api/downloads")
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(20.0, connect=8.0)
                ) as client:
                    response = await client.post(
                        url, headers=headers, json={"repo": required["repo"]}
                    )
                    response.raise_for_status()
                    payload = response.json()
                if isinstance(payload, dict) and payload.get("already_cached"):
                    state = "cached"
                    detail = f"{required['label']} is ready"
                    job_id = None
                else:
                    job = payload.get("job") if isinstance(payload, dict) else None
                    state = str((job or {}).get("state") or "queued")
                    detail = f"{required['label']} resumable download accepted"
                    job_id = (job or {}).get("id")
                self.targets[key] = {
                    **eligibility,
                    "state": state,
                    "detail": detail,
                    **({"job_id": job_id} if job_id else {}),
                    "checked_at": time.time(),
                }
            except (httpx.HTTPError, ValueError, TypeError, OSError) as exc:
                self.targets[key] = {
                    **eligibility,
                    "state": "error",
                    "detail": (str(exc).strip() or type(exc).__name__)[:220],
                    "checked_at": time.time(),
                }
