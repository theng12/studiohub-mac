"""Recipe engine + agentic director (SPEC intelligence plane).

A recipe is a linear chain of steps. Each step runs on one studio; template
variables carry context forward:
  {{brief}}      — the run's input text
  {{prev.text}}  — previous chat step's reply (or previous prompt)
  {{prev.artifact}} — previous step's artifact path

Chat steps call Chat Studio's OpenAI-compatible endpoint synchronously and
contribute TEXT (e.g. expand a brief into an image prompt). Generation steps
(image/voice/music/video) run as 1-item batches through the broker, so they
inherit the memory governor, retries and ledger provenance automatically.

The director asks Chat Studio to WRITE a recipe from a plain-English brief:
the Hub feeds it the live studio + downloaded-model inventory and a strict
JSON shape, then validates whatever comes back. LLM output is fallible — the
director returns the recipe for review by default; auto_run is opt-in.
"""

import asyncio
import json
import re
import time
import uuid

import httpx

from . import broker
from .peers import studio_request

runs: dict[str, dict] = {}

RECIPE_TIMEOUT_S = 1800  # a whole chain may include slow video steps


def _monitor():
    from .main import monitor
    return monitor


def _template(text: str, ctx: dict) -> str:
    return re.sub(
        r"\{\{\s*([a-z.]+)\s*\}\}",
        lambda m: str(ctx.get(m.group(1), m.group(0))), text or "")


async def _chat(client: httpx.AsyncClient, model: str, prompt: str,
                system: str | None = None) -> str:
    chat = next((s for s in _monitor().registry if s["modality"] == "chat"
                 and _monitor().status.get(s["id"], {}).get("status") == "up"), None)
    if chat is None:
        raise RuntimeError("no chat studio is up")
    messages = ([{"role": "system", "content": system}] if system else []) \
        + [{"role": "user", "content": prompt}]
    url, headers = studio_request(chat, "/v1/chat/completions")
    r = await client.post(
        url,
        json={"model": model, "messages": messages, "stream": False},
        headers=headers,
        timeout=httpx.Timeout(connect=5, read=600, write=30, pool=5))
    if r.status_code >= 400:
        raise RuntimeError(f"chat HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"]


def validate_recipe(recipe: dict) -> str | None:
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not steps:
        return "recipe.steps must be a non-empty list"
    for i, st in enumerate(steps):
        mod = st.get("modality")
        if mod != "chat" and mod not in broker.MODALITY:
            return f"step {i}: bad modality {mod!r}"
        if not st.get("model"):
            return f"step {i}: model is required"
        if not st.get("prompt"):
            return f"step {i}: prompt is required"
    return None


async def run_recipe(recipe: dict, brief: str = "") -> str:
    err = validate_recipe(recipe)
    if err:
        raise ValueError(err)
    run_id = uuid.uuid4().hex[:10]
    runs[run_id] = {
        "id": run_id, "recipe": recipe.get("name", "unnamed"),
        "created_at": time.time(), "state": "running",
        "steps": [{"modality": s["modality"], "model": s["model"],
                   "state": "pending", "output": None, "error": None}
                  for s in recipe["steps"]],
    }
    asyncio.create_task(_run(run_id, recipe, brief))
    return run_id


async def _run(run_id: str, recipe: dict, brief: str):
    run = runs[run_id]
    ctx = {"brief": brief, "prev.text": brief, "prev.artifact": ""}
    async with httpx.AsyncClient() as client:
        for i, step in enumerate(recipe["steps"]):
            srec = run["steps"][i]
            srec["state"] = "running"
            prompt = _template(step["prompt"], ctx)
            try:
                if step["modality"] == "chat":
                    text = await _chat(client, step["model"], prompt,
                                       step.get("system"))
                    ctx["prev.text"] = text
                    srec["output"] = text
                else:
                    sub = broker.submit_batch({
                        "modality": step["modality"], "model": step["model"],
                        "items": [{"prompt": prompt,
                                   "params": step.get("params") or {}}],
                    })
                    if "error" in sub:
                        raise RuntimeError(sub["error"])
                    batch = broker.batches[sub["batch_id"]]
                    deadline = time.time() + RECIPE_TIMEOUT_S
                    while time.time() < deadline:
                        item = batch["items"][0]
                        if item["state"] in ("done", "error", "cancelled"):
                            break
                        await asyncio.sleep(2)
                    item = batch["items"][0]
                    if item["state"] != "done":
                        raise RuntimeError(item.get("error") or "step timed out")
                    ctx["prev.artifact"] = item["artifact_path"] or ""
                    ctx["prev.text"] = prompt
                    srec["output"] = {
                        "artifact_path": item["artifact_path"],
                        "artifact_url": item["artifact_url"],
                        "asset_id": item["asset_id"],
                        "batch_id": sub["batch_id"],
                    }
                srec["state"] = "done"
            except Exception as e:
                srec["state"] = "error"
                srec["error"] = str(e)
                run["state"] = "error"
                return
    run["state"] = "done"


DIRECTOR_SYSTEM = """You are the director of a local AI studio hub. You turn a
user's brief into a recipe: a JSON object {"name": str, "steps": [...]} where
each step is {"modality": "chat"|"image"|"voice"|"music"|"video",
"model": "<repo from the inventory>", "prompt": str, "params": {}}.
Rules:
- Use ONLY models from the inventory below (repo strings, exactly as given).
- Steps run in order. Use {{brief}} for the user's brief, {{prev.text}} for
  the previous chat step's output.
- A good pattern: one chat step to craft a vivid prompt, then generation steps.
- Keep it minimal: 1-3 steps unless the brief demands more.
Reply with ONLY the JSON object, no prose, no markdown fences."""


def _check_pairing(recipe: dict, downloaded: list[dict]) -> str | None:
    """Small LLMs love pairing a music model with a voice step — verify every
    step's model actually belongs to that step's modality."""
    by_repo = {m["repo"]: m["hub_modality"] for m in downloaded}
    for i, st in enumerate(recipe["steps"]):
        actual = by_repo.get(st["model"])
        if actual is None:
            return (f"step {i}: model {st['model']!r} is not in the inventory "
                    f"of downloaded models")
        if actual != st["modality"]:
            return (f"step {i}: model {st['model']!r} is a {actual} model but "
                    f"the step's modality is {st['modality']!r}")
    return None


async def direct(brief: str, chat_model: str | None = None) -> dict:
    mon = _monitor()
    agg = await mon.aggregate_catalog()
    # only models actually downloaded somewhere (hub_cached), deduped by repo
    seen, downloaded = set(), []
    for m in agg["models"]:
        if (m.get("hub_cached") or m.get("is_cloud")) and m["repo"] not in seen:
            seen.add(m["repo"])
            downloaded.append(m)
    if chat_model is None:
        chat_models = [m["repo"] for m in downloaded if m["hub_modality"] == "chat"]
        if not chat_models:
            return {"error": "no downloaded chat model available to act as director"}
        chat_model = chat_models[0]
    # Grouped by modality with explicit headers — small local LLMs mispair
    # models when given a flat list, but follow section boundaries well.
    sections = []
    for mod in ("chat", "image", "voice", "music", "video"):
        repos = [m["repo"] for m in downloaded if m["hub_modality"] == mod][:6]
        if repos:
            sections.append(
                f"{mod.upper()} steps may ONLY use these models:\n" +
                "\n".join(f"  - {r}" for r in repos))
    inventory = "\n\n".join(sections)
    prompt = f"Inventory of available models:\n\n{inventory}\n\nBrief: {brief}"

    last_error, raw, recipe = None, "", None
    async with httpx.AsyncClient() as client:
        for attempt in range(2):  # one self-repair retry with feedback
            ask = prompt if last_error is None else (
                f"{prompt}\n\nYour previous recipe was rejected: {last_error}\n"
                f"Fix it. Remember: each step's model MUST be listed under that "
                f"step's modality in the inventory. Reply with only the JSON.")
            raw = await _chat(client, chat_model, ask, system=DIRECTOR_SYSTEM)
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                last_error = "reply contained no JSON object"
                continue
            try:
                recipe = json.loads(match.group(0))
            except json.JSONDecodeError as e:
                last_error = f"invalid JSON: {e}"
                continue
            last_error = validate_recipe(recipe) or _check_pairing(recipe, downloaded)
            if last_error is None:
                return {"recipe": recipe, "chat_model": chat_model,
                        "attempts": attempt + 1}
    return {"error": f"director failed after retry: {last_error}",
            "recipe": recipe, "raw": raw[:500]}
