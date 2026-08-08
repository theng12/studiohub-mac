"""Refuse GenStudio-originated cloud work at the Studio Hub door.

Studio Hub KH is a *local* fleet controller: it exists to run models on the
Macs it controls. GenStudio KH owns cloud generation and runs it in its own
provider adapters. Until now that split held only because GenStudio chose to
route cloud away from the Hub — a convention on the sender's side, undone by
one routing change in another repository. This module makes it a property of
the Hub instead: a GenStudio-originated job naming a cloud model is refused at
submission, before anything is queued or persisted, with the offending model id
in the error.

What this is NOT:

* It is not a removal of cloud support. A local operator submitting a cloud
  model from the Hub's own Jobs tab is unaffected, and so is any other caller
  that carries no GenStudio identity. Video Studio's ``fal:`` / ``kie:`` /
  ``replicate:`` routes and Chat Studio's paid/free tiers keep working for
  them. The rule is about the *source* of the work.
* It is not a second download/memory governor. A local model that is still
  downloading keeps waiting exactly as it did.
* It never applies to Render. See ``LOCAL_ONLY_MODALITIES`` below.
"""

from __future__ import annotations

import logging

from .monitor import LOCAL_ONLY_MODALITIES

log = logging.getLogger("studiohub.cloud_guard")

REFUSAL_CODE = "GENSTUDIO_CLOUD_WORK_REFUSED"

# GenStudio stamps every customer job it owns with a job/attempt id pair, which
# ``execution_identity`` then validates. Those two ids are the authoritative
# origin marker: no local operator produces them.
_IDENTITY_FIELDS = ("genstudio_job_id", "genstudio_attempt_id")

# The broker already treats this label prefix as "customer job from GenStudio"
# (see ``_supports_genstudio_voice_evidence``). Kept as a second, independent
# marker so a GenStudio submission that omits execution identity is still
# recognised.
GENSTUDIO_LABEL_PREFIX = "genstudio-kh:"

# Id schemes that are cloud by construction. The separator is load-bearing:
# ``fal:fal-ai/kling-video/...`` is a Video Studio cloud route, while
# ``fal/AuraFlow-v0.3`` is a LOCAL image model whose Hugging Face owner happens
# to be named "fal". Only the colon forms belong here.
CLOUD_ID_SCHEMES = ("provider:", "fal:", "kie:", "replicate:")

# Cloud inference vendors that Image Studio encodes as a repo *namespace*
# rather than a scheme (``cloudflare/flux-1-schnell``). These are the vendors
# actually observed in the fleet's catalogues; each one is an inference API,
# never a Hugging Face account the Hub would download from.
CLOUD_ID_NAMESPACES = (
    "cloudflare/", "gemini/", "together/", "nebius/", "pollinations/",
    "huggingface/",
)

# What a studio reports in ``provider`` for a model it runs itself. Image
# Studio says "local" on every local entry; the other studios omit the field.
# Anything else names an external vendor.
LOCAL_PROVIDER_VALUES = {"", "local", "none", "hub", "on-device", "device"}

# Chat Studio does not set ``is_cloud`` on its catalogue entries; it declares
# the lane on the job instead. "local" runs on a Mac, "free" and "paid" are
# provider calls.
CLOUD_COST_TIERS = {"free", "paid"}


def is_genstudio_origin(envelope: object) -> bool:
    """Whether GenStudio KH — not a local operator — submitted this work.

    Accepts either a raw submission envelope or a stored batch, since both
    carry the same two markers. Either marker alone is enough; a local
    operator produces neither.
    """
    if not isinstance(envelope, dict):
        return False
    if str(envelope.get("label") or "").startswith(GENSTUDIO_LABEL_PREFIX):
        return True
    nested = envelope.get("genstudio_execution")
    if isinstance(nested, dict) and _has_identity(nested):
        return True
    return _has_identity(envelope)


def _has_identity(source: dict) -> bool:
    return any(str(source.get(field) or "").strip() for field in _IDENTITY_FIELDS)


def _id_cloud_reason(model: str) -> str | None:
    lowered = model.strip().lower()
    for scheme in CLOUD_ID_SCHEMES:
        if lowered.startswith(scheme):
            return f"its id uses the cloud scheme '{scheme}'"
    for namespace in CLOUD_ID_NAMESPACES:
        if lowered.startswith(namespace):
            return f"its id is in the cloud-provider namespace '{namespace}'"
    return None


def _entry_cloud_reason(entry: dict) -> str | None:
    """Why one worker catalogue entry describes a cloud model, if it does.

    Deliberately reads four independent signals, because ``is_cloud`` alone is
    not trustworthy: Voice Studio's provider entries carried ``is_cloud=None``
    and announced themselves only through ``cache.state`` and ``kind``.
    """
    if entry.get("is_cloud"):
        return "its worker catalogue entry sets is_cloud"
    if str(entry.get("kind") or "").strip().lower() == "cloud":
        return "its worker catalogue entry sets kind=cloud"
    cache = entry.get("cache")
    state = cache.get("state") if isinstance(cache, dict) else cache
    if str(state or "").strip().lower() == "cloud":
        return "its worker catalogue entry reports cache.state=cloud"
    provider = str(
        entry.get("provider") or entry.get("cloud_provider") or ""
    ).strip().lower()
    if provider and provider not in LOCAL_PROVIDER_VALUES:
        return f"its worker catalogue entry names the external provider '{provider}'"
    return None


def cloud_reason(
    model: object,
    *,
    modality: object = None,
    entries: object = None,
    cost_tier: object = None,
) -> str | None:
    """Return why this workload is cloud, or ``None`` if it is local.

    RENDER IS EXEMPT, AND EXEMPT FIRST. Render Studio sets ``is_cloud=true`` on
    ``episode-assembly-v1`` purely to bypass the broker's download/memory
    governor — it is a local FFmpeg assembly step running on a Mac in the
    fleet, not a hosted model. ``LOCAL_ONLY_MODALITIES`` is the single place
    that fact is recorded, and this function honours it before it looks at
    anything else, exactly as ``monitor.is_cloud_lane`` does. Render is
    additionally safe by id: ``episode-assembly-v1`` matches no cloud scheme
    and no cloud namespace.
    """
    if str(modality or "").strip().lower() in LOCAL_ONLY_MODALITIES:
        return None
    reason = _id_cloud_reason(str(model or ""))
    if reason:
        return reason
    tier = str(cost_tier or "").strip().lower()
    if tier in CLOUD_COST_TIERS:
        return f"it was submitted on the '{tier}' cost tier, which is a provider call"
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        reason = _entry_cloud_reason(entry)
        if reason:
            return reason
    return None


def _cached_entries(model: object, modality: object) -> list[dict]:
    """Last-good worker catalogue entries for this model, without any I/O.

    Submission must never fan out to workers, so this reads the monitor's
    durable cache only. A cold cache simply means the id-scheme rule decides
    on its own — which today already covers every cloud entry in the fleet.
    """
    try:
        from .main import monitor

        matches = monitor.cached_catalog_entries(
            str(model or ""),
            modality=str(modality) if modality else None,
        )
    except Exception:  # a submission must not fail because of a cache read
        return []
    return [match["entry"] for match in matches if isinstance(match.get("entry"), dict)]


def refusal(
    envelope: object,
    *,
    model: object,
    modality: object = None,
    cost_tier: object = None,
) -> str | None:
    """The refusal message for GenStudio cloud work, or ``None`` to allow.

    Callers must apply this at the API boundary, before the work is queued: a
    refused job must never be accepted and then failed downstream.
    """
    if not is_genstudio_origin(envelope):
        return None
    reason = cloud_reason(
        model,
        modality=modality,
        entries=_cached_entries(model, modality),
        cost_tier=cost_tier,
    )
    if reason is None:
        return None
    message = (
        f"Studio Hub does not accept cloud work from GenStudio. "
        f"'{model}' is a cloud model because {reason}. Studio Hub runs the "
        f"local Mac fleet only — run this model on GenStudio's own provider "
        f"adapters instead."
    )
    log.warning(
        "refused GenStudio cloud submission: model=%r modality=%r reason=%s",
        str(model or ""), str(modality or ""), reason,
    )
    return message


def catalog_cloud_reason(model: dict) -> str | None:
    """Why a pushed GenStudio fleet-catalog row is cloud, if it is.

    The fleet catalog asks this Hub to *cache* a model on its Macs, so a cloud
    row is meaningless here on top of being unwanted. Rows are keyed by
    ``sibling_studio`` and ``repo``.
    """
    if not isinstance(model, dict):
        return None
    return cloud_reason(
        model.get("repo"),
        modality=model.get("modality") or model.get("sibling_studio"),
        entries=(model,),
    )
