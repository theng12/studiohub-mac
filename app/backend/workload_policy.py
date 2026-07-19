"""Central, conservative production requirements for routed workloads.

Studio catalogs describe what their engine can *technically* attempt.  The Hub
also owns the stricter product policy that keeps a customer-facing workload
reliable across a heterogeneous fleet.  Put such floors here rather than
changing worker catalogs: one policy applies to local and remote workers, and
an 8 GB Mac can still accept other modalities such as image generation.
"""

from __future__ import annotations


# Qwen3-TTS 0.6B CustomVoice can sometimes load on an 8 GB Apple-silicon Mac,
# but it does not leave dependable operating headroom once the Voice Studio
# service, the operating system, and a real generation are included.  GenStudio
# standard voice therefore requires a 16 GB worker as a service policy.
_MIN_MACHINE_MEMORY_GB_BY_REPO_PREFIX = {
    "mlx-community/qwen3-tts-12hz-0.6b-customvoice": 16.0,
    "qwen/qwen3-tts-12hz-0.6b-customvoice": 16.0,
}


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

    identifiers = [requested_model, catalog_entry.get("repo"),
                   *(catalog_entry.get("aliases") or [])]
    for identifier in identifiers:
        normalized = str(identifier or "").strip().lower()
        for prefix, minimum in _MIN_MACHINE_MEMORY_GB_BY_REPO_PREFIX.items():
            if normalized.startswith(prefix):
                values.append(minimum)

    return max(values) if values else None
