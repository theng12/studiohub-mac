"""Central, conservative production requirements for routed workloads.

Studio catalogs describe what their engine can *technically* attempt.  The Hub
also owns the stricter product policy that keeps a customer-facing workload
reliable across a heterogeneous fleet.  Put such floors here rather than
changing worker catalogs: one policy applies to local and remote workers, and
an 8 GB Mac can still accept other modalities such as image generation.
"""

from __future__ import annotations


# The 0.6B 8-bit Qwen3-TTS checkpoints are supported on 8 GB Apple-silicon
# Macs. Voice Studio's own cold-load guard requires 3.15 GB free (1.9 GB model
# plus 1.25 GB headroom), so the Hub rounds that up to 3.2 GB before dispatch.
# This preserves 8 GB capacity without sending a job that the worker will
# immediately decline under normal memory pressure.
_MIN_MACHINE_MEMORY_GB_BY_REPO_PREFIX = {
    "mlx-community/qwen3-tts-12hz-0.6b-": 8.0,
    "qwen/qwen3-tts-12hz-0.6b-": 8.0,
}
_MIN_FREE_MEMORY_GB_BY_REPO_PREFIX = {
    "mlx-community/qwen3-tts-12hz-0.6b-": 3.2,
    "qwen/qwen3-tts-12hz-0.6b-": 3.2,
}


def _identifiers(requested_model: str, catalog_entry: dict) -> list[str]:
    return [str(value or "").strip().lower() for value in (
        requested_model, catalog_entry.get("repo"),
        *(catalog_entry.get("aliases") or []),
    )]


def required_total_memory_gb(requested_model: str, catalog_entry: dict) -> float | None:
    """Return the stricter catalog or Hub production memory requirement.

    ``requested_model`` may be one of a catalog entry's aliases, so inspect
    both it and every advertised identifier.  Unknown models retain the
    worker's own catalog requirement unchanged.
    """
    values: list[float] = []
    catalog_minimum = catalog_entry.get("min_unified_memory_gb")
    try:
        if catalog_minimum is not None:
            values.append(float(catalog_minimum))
    except (TypeError, ValueError):
        pass

    for normalized in _identifiers(requested_model, catalog_entry):
        for prefix, minimum in _MIN_MACHINE_MEMORY_GB_BY_REPO_PREFIX.items():
            if normalized.startswith(prefix):
                values.append(minimum)

    return max(values) if values else None


def required_free_memory_gb(requested_model: str, catalog_entry: dict) -> float | None:
    """Return a conservative live-free-memory floor when one exists.

    ``size_gb`` is deliberately not considered here. Studio catalogs define it
    as download/disk size, which can be much larger than the memory mapped by a
    quantized or offloaded runtime. Workers may publish an explicit admission
    floor as ``min_free_memory_gb``; Hub production overrides can raise it.
    """
    values: list[float] = []
    catalog_minimum = catalog_entry.get("min_free_memory_gb")
    try:
        if catalog_minimum is not None:
            values.append(float(catalog_minimum))
    except (TypeError, ValueError):
        pass

    values.extend(
        minimum
        for normalized in _identifiers(requested_model, catalog_entry)
        for prefix, minimum in _MIN_FREE_MEMORY_GB_BY_REPO_PREFIX.items()
        if normalized.startswith(prefix)
    )
    return max(values) if values else None
