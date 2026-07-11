# Integrating a client (e.g. Story Studio KH) with Studio Hub KH

This document is written to be handed to a *separate* coding session that is
building or modifying **Story Studio KH** so it uses **Studio Hub KH** as its
one and only backend for generation. Everything the client needs is here.

> **Is it plug-and-play?** No. Story Studio must add a small HTTP client for the
> Hub API (the code below). But it's plain REST/JSON — no SDK, no websockets
> required. Once added, Story Studio stores **one address + one token** instead
> of any studio IPs.

---

## 1. Mental model

- The **Hub** is a single FastAPI service (default `http://<hub-host>:47873`).
- It fronts a fleet of "Studio" servers (Image / Chat / Voice / Music / Video),
  possibly spread across several Macs on a Tailscale network.
- Story Studio should **never** talk to individual studios. It talks to the Hub:
  - ask *what can I generate* (which modalities/models are available & downloaded),
  - **submit a batch of prompts**, and
  - **get results** (poll, or receive a webhook), each with a downloadable artifact.
- The Hub handles routing to a machine that has the model, load-balancing across
  machines, memory limits, retries, and recording every result.

**Prompt-field convention:** for *every* modality, put the text in `prompt`.
The Hub maps it to each studio's real field (e.g. Voice wants `text`). You never
special-case per modality.

**Per-model params are opaque pass-through.** The Hub does NOT define a unified
schema. Model-specific knobs (image `width`/`height`/`steps`/`guidance`, voice
`voice`/`language`/`speed`, video `frames`/`fps`, …) go in each item's `params`
(or batch-wide `sharedParams`) and are forwarded to the studio verbatim. To learn
a model's params, read that studio's catalog via the gateway
(`GET /studio/<id>/api/catalog`) or its README. Sensible defaults apply if omitted.

---

## 2. Connect

- **Base URL:** the Hub machine's address. Locally `http://localhost:47873`;
  over Tailscale `http://<tailscale-ip>:47873` (the Hub's Remote tab lists them).
- **Auth:** requests from *other machines* need the Hub token. Send it as any of:
  - `Authorization: Bearer <token>`
  - `X-Hub-Token: <token>`
  - `?token=<token>` query param
  - Requests from the Hub's own machine (loopback) need no token.
- Get the token from the Hub dashboard → **Remote** tab (shown only on the Hub
  machine). Store it in Story Studio's config/secrets.

Health check (no token needed): `GET /api/health` → `{ "ok": true, ... }`.

---

## 3. Discover capabilities

### Which studios/machines are alive
`GET /api/hub/studios` →
```json
{ "studios": [
  { "id": "image", "modality": "image", "machine": "local", "status": "up",
    "host": "127.0.0.1", "port": 47868, "app_version": "1.17.3" },
  { "id": "image@mac-studio", "modality": "image", "machine": "mac-studio",
    "status": "down", "host": "100.x.y.z", "port": 47868 }
] }
```
Use `status === "up"` to know what's currently runnable.

### Which models exist and where they're downloaded
`GET /api/hub/models?modality=image&downloaded=true` →
```json
{ "count": 2, "models": [
  { "repo": "AITRADER/FLUX2-klein-4B-mlx-4bit", "label": "FLUX.2 klein 4B — MLX 4-bit",
    "modality": "image", "family_label": "FLUX.2 klein",
    "size_gb": 2.3, "min_unified_memory_gb": 8, "is_cloud": false,
    "downloaded": true, "cached_on": ["local", "mac-studio"],
    "machines": [ { "studio": "image", "machine": "local", "cached": true } ] }
] }
```
- Deduped by `repo`. `cached_on` = machines that actually have it downloaded.
- Filters: `modality`, `q` (search), `downloaded=true|false`.
- **Pick a model by its `repo` string.** That's what you submit.
- NOTE: this call can take a few seconds if some fleet machines are offline
  (it waits on their catalogs). Cache the result in Story Studio for a minute.

---

## 4. Submit work — Swarm Batch

`POST /api/hub/jobs` with a batch envelope:
```json
{
  "modality": "image",
  "model": "AITRADER/FLUX2-klein-4B-mlx-4bit",
  "label": "storystudio-kh",
  "webhook": "http://<storystudio-host>:<port>/hub-callback",
  "sharedParams": { "width": 1024, "height": 1024, "steps": 4 },
  "items": [
    { "prompt": "a lighthouse at dawn, oil painting", "seed": 42 },
    { "prompt": "a red fox in the snow", "params": { "steps": 6 } }
  ]
}
```
- `modality`: one of `image | voice | music | video` (chat is different, see §7).
- `model`: a `repo` from `/api/hub/models`.
- `items`: 1..N. Each has `prompt` (always), optional `seed`, optional `params`.
- `sharedParams` merge into every item (item `params` win on conflict).
- `label`: free string; who submitted (shows in the Hub dashboard).
- `webhook`: optional; the Hub POSTs the finished batch here (see §6). Recommended.
- `routing`: optional; `"pool"` (default, any machine with the model) or
  `"studio:<id>"` to pin to one.

Response: `{ "batch_id": "0e13ca4f16", "items": 2 }`.

The Hub queues the items and dispatches each to a free studio that **has that
model downloaded**, across all machines. Faster machines naturally do more.

---

## 4b. Reference images (img2img / edit)

For continuity / style-ref renders, add `reference_images` to an image item's
`params`. Presence of it makes the job img2img/edit instead of txt2img.

```jsonc
"items": [{
  "prompt": "...",
  "params": {
    "width": 1080, "height": 1920, "steps": 28, "image_strength": 0.6,
    "ref_mode": "img2img" | "edit",     // optional; Hub infers from model caps if omitted
    "reference_images": [               // primary first
      { "b64": "<base64 image>", "mime": "image/png" }
      // OR { "asset_id": "<from /api/hub/assets/upload>" }
      // OR { "url": "http://<tailnet>/…" }
    ]
  }
}]
```

- **Single reference:** the Hub forwards `reference_images[0]` and ignores the
  rest — pre-select your primary anchor client-side.
- **Capability:** only models whose catalog `capabilities` include `img2img` /
  `edit` are eligible; others return a clean item `error` (not a wrong txt2img).
  `GET /api/hub/models` → each model's availability; capabilities come from the
  studio catalog (`GET /studio/image/api/catalog`).
- **Upload once, reference many** (continuity): `POST /api/hub/assets/upload`
  (multipart `file`) → `{ asset_id }`; then reference `{ "asset_id": … }` across
  every scene. Avoids re-sending the character-sheet megabytes per item.
- Large base64 bodies are accepted (no small cap); prefer JPEG for refs when
  lossless isn't needed.
- Everything else (poll / webhook / `artifact_url`) is identical to txt2img.

## 5. Get results by polling

`GET /api/hub/jobs/<batch_id>` →
```json
{
  "id": "0e13ca4f16", "modality": "image", "model": "...", "label": "storystudio-kh",
  "total": 2, "queued": 0, "running": 1, "done": 1, "error": 0,
  "cancelled": false, "cancelled_items": 0, "governor_note": null,
  "items": [
    { "index": 0, "state": "done", "prompt": "...", "seed": 42,
      "studio": "image", "artifact_url": "http://127.0.0.1:47868/api/generate/jobs/<jid>/image",
      "artifact_path": "/…/output/abc.png", "asset_id": "8694eddec4f6", "error": null },
    { "index": 1, "state": "running", ... }
  ]
}
```
- Item `state`: `queued | running | done | error | cancelled`.
- Poll every ~2–3s until `queued + running === 0`.
- This endpoint **survives Hub restarts** (batches persist in the Hub's DB).
- `governor_note` (string) explains a stall, e.g. model not downloaded anywhere
  for that modality, or waiting for memory.
- Cancel: `DELETE /api/hub/jobs/<batch_id>`.

---

## 6. Get results by webhook (preferred — no polling)

If you pass `webhook` in the envelope, the Hub POSTs to it once the batch
reaches a terminal state (all items done/error/cancelled). Payload:
```json
{
  "id": "0e13ca4f16", "label": "storystudio-kh", "modality": "image",
  "total": 2, "done": 2, "error": 0, "cancelled_items": 0,
  "items": [
    { "index": 0, "state": "done",
      "artifact_url": "http://127.0.0.1:47868/api/generate/jobs/<jid>/image",
      "artifact_path": "/…/abc.png", "asset_id": "…", "error": null }
  ]
}
```
Story Studio must expose an HTTP endpoint (the `webhook` URL) to receive this
POST. Match results back to your request by `label` + `id` (and `index`).

---

## 6b. A whole render as ONE batch (recommended for multi-scene stories)

**Submit all scenes of a story in a single batch, not one batch per scene.** The
Hub then owns the queue and work-steals every scene across the whole fleet, and
the dashboard shows a single **`n / N` progress** line for the story (with the
machine each scene is running on, elapsed, and ETA). Submitting one 1-item batch
per scene works too, but the Hub can't show story-level progress — it just sees N
unrelated jobs.

```json
POST /api/hub/jobs
{
  "modality": "image",
  "model": "AITRADER/FLUX2-klein-4B-mlx-4bit",
  "label": "storystudio:<story-id-or-title>",
  "sharedParams": { "width": 1024, "height": 1024 },
  "itemWebhook": "http://<storystudio-host>:<port>/hub-item",
  "webhook":     "http://<storystudio-host>:<port>/hub-done",
  "items": [
    { "prompt": "scene 1 …", "seed": 1 },
    { "prompt": "scene 2 …", "seed": 2 }
    /* … all 120 scenes … */
  ]
}
```

**Stream results back as each scene finishes** — pass `itemWebhook`. The Hub
POSTs one small payload the moment *each* item reaches a terminal state (so you
don't wait for all 120, and you don't have to poll):
```json
{
  "batch_id": "0e13ca4f16", "label": "storystudio:my-story",
  "index": 7, "state": "done", "machine": "macmini-m4-16gb-003-256",
  "studio": "image@macmini-m4-16gb-003-256",
  "artifact_url": "http://100.79.198.73:47868/api/generate/jobs/<jid>/image",
  "artifact_path": "/…/scene7.png", "asset_id": "…", "duration_s": 44.8,
  "error": null,
  "done": 8, "total": 120
}
```
- `index` maps straight back to your scene order; `done`/`total` is the live
  story progress so you can render your own `8 / 120` bar without polling.
- `itemWebhook` (per scene) and `webhook` (once, on whole-batch completion) are
  independent — use either or both. If you can't run a webhook receiver, poll
  `GET /api/hub/jobs/<batch_id>` instead; its `items[]` fill in `artifact_url` /
  `state` as scenes finish, and it survives Hub restarts.
- Ordering/back-pressure is the Hub's job now: it dispatches as fast as free
  studios (with the model) appear, fastest machines naturally taking more.
- Cancel the whole story with `DELETE /api/hub/jobs/<batch_id>`.

---

## 7. Fetching the generated artifact

Each done item has `artifact_url` — a **full URL on the studio that produced it**
(could be a remote machine on the tailnet). `GET` it to download the bytes
(image/audio/video). Story Studio must be on the same Tailscale network to reach
remote artifact URLs. `artifact_path` is the on-disk path on that machine (useful
only if Story Studio runs on the same box).

---

## 8. Chat / LLM text (different from batch)

Chat is synchronous and OpenAI-compatible. Use the Hub **gateway** to reach it:

`POST /studio/chat/v1/chat/completions`
```json
{ "model": "mlx-community/Llama-3.2-3B-Instruct-4bit",
  "messages": [ { "role": "user", "content": "Expand this beat into a scene…" } ],
  "stream": false }
```
The gateway (`ANY /studio/<id>/<path>`) proxies to the right studio and streams
responses (SSE included). Great for prompt expansion, outlines, dialogue — then
feed the text into image/voice/etc. batches.

---

## 9. Optional: let the Hub plan a whole pipeline

`POST /api/hub/director { "brief": "...", "auto_run": true }` uses a local chat
model to turn a plain-English brief into a multi-studio recipe and (optionally)
run it. Returns the recipe (and a `run_id` if auto_run). Recipe run status:
`GET /api/hub/recipes/runs/<run_id>`. This is optional — Story Studio can also
orchestrate steps itself via §4–8.

---

## 10. Assets ledger (reproducibility)

Every generated item is recorded with prompt + model + resolved seed + params.
`GET /api/hub/assets?q=&modality=&batch_id=` returns them, each with
`artifact_url`, `seed`, `params` — so any output can be reproduced or shown in a
library. Story Studio can use `batch_id` to fetch exactly the assets it created.

---

## 11. Minimal client

### JavaScript
```javascript
const HUB = "http://<hub-host>:47873";
const TOKEN = process.env.HUB_TOKEN; // from the Hub's Remote tab
const H = { "Content-Type": "application/json", "X-Hub-Token": TOKEN };

async function pickModel(modality) {
  const r = await fetch(`${HUB}/api/hub/models?modality=${modality}&downloaded=true`, { headers: H });
  const { models } = await r.json();
  if (!models.length) throw new Error(`no downloaded ${modality} model in the fleet`);
  return models[0].repo;
}

async function generate(modality, prompts, sharedParams = {}) {
  const model = await pickModel(modality);
  const r = await fetch(`${HUB}/api/hub/jobs`, {
    method: "POST", headers: H,
    body: JSON.stringify({
      modality, model, label: "storystudio-kh", sharedParams,
      items: prompts.map(p => ({ prompt: p })),
      // webhook: "http://<storystudio-host>:PORT/hub-callback"  // optional
    })
  });
  const { batch_id } = await r.json();
  // poll (or use the webhook instead)
  for (;;) {
    await new Promise(s => setTimeout(s, 2500));
    const b = await (await fetch(`${HUB}/api/hub/jobs/${batch_id}`, { headers: H })).json();
    if (b.queued + b.running === 0)
      return b.items.map(i => ({ index: i.index, state: i.state, url: i.artifact_url, error: i.error }));
  }
}
```

### Python
```python
import time, httpx
HUB, TOKEN = "http://<hub-host>:47873", "<token>"
H = {"X-Hub-Token": TOKEN}

def pick_model(client, modality):
    m = client.get(f"{HUB}/api/hub/models", params={"modality": modality, "downloaded": True}, headers=H).json()
    if not m["models"]:
        raise RuntimeError(f"no downloaded {modality} model in the fleet")
    return m["models"][0]["repo"]

def generate(prompts, modality="image", shared=None):
    with httpx.Client(timeout=30) as c:
        model = pick_model(c, modality)
        bid = c.post(f"{HUB}/api/hub/jobs", headers=H, json={
            "modality": modality, "model": model, "label": "storystudio-kh",
            "sharedParams": shared or {}, "items": [{"prompt": p} for p in prompts],
        }).json()["batch_id"]
        while True:
            time.sleep(2.5)
            b = c.get(f"{HUB}/api/hub/jobs/{bid}", headers=H).json()
            if b["queued"] + b["running"] == 0:
                return [{"index": i["index"], "state": i["state"], "url": i["artifact_url"]} for i in b["items"]]
```

---

## 12. What to build into Story Studio (checklist)

1. **Config**: `HUB_URL` + `HUB_TOKEN` (replace any stored studio IPs with these).
2. **Capability cache**: on start / periodically, `GET /api/hub/studios` and
   `/api/hub/models` so the UI only offers modalities/models that are up & downloaded.
3. **Submit**: build the batch envelope (§4); always use `prompt`; put model
   params in `params`/`sharedParams`.
4. **Results**: implement a `webhook` receiver (preferred) OR poll `/api/hub/jobs/{id}`.
5. **Fetch artifacts**: download `artifact_url` (be on the tailnet for remote ones).
6. **Chat**: use the gateway (`/studio/chat/v1/chat/completions`) for text steps.
7. **Errors/edge**: handle `governor_note`, item `state === "error"` (+ `error`),
   and "no downloaded model" (offer to trigger a download via
   `POST /api/hub/broadcast/download {repo, studios?}` if you want that in-app).

---

## 13. Gotchas

- **Token required off-box.** A 401 means missing/wrong token (loopback is exempt).
- **Model must be downloaded somewhere.** Submitting a model no machine has cached
  leaves items queued with a `governor_note`. Check `/api/hub/models` first.
- **Local vs cloud models.** `is_cloud: true` models don't use local RAM and aren't
  gated by memory; local models are. Either works as a `model` value.
- **`/api/hub/models` latency** with offline fleet machines — cache it client-side.
- **Artifacts live on the producing machine.** Story Studio needs tailnet access to
  fetch remote `artifact_url`s.
- **Batch queue is durable; in-flight items are re-run after a Hub restart.** Idempotent
  by design (results keyed by new artifact + recorded in the ledger).

Full API reference: the Hub's `README.md` and `SPEC.md`.
