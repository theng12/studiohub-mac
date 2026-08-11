# Agent-Hub Rolling Updates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each site controller update agent Hubs one at a time, using the first eligible agent as the canary and continuing safely after per-machine failures.

**Architecture:** Keep the existing `POST /api/hub/maintenance/hub-updates` job contract and durable item state unchanged. Replace only the controller's concurrent fan-out with ordered iteration so each `_update_hub_one` call reaches a terminal state before the next starts; GenStudio continues to initiate and observe the same Hub-owned job IDs.

**Tech Stack:** Python 3.12, asyncio, pytest, FastAPI, existing Studio Hub durable fleet-operation state.

## Global Constraints

- GenStudio remains the authenticated global initiator; Studio Hub remains the sole site-local executor.
- The first item in the Hub-owned update job is the canary and must reach terminal state before later items begin.
- A failed or unreachable agent is recorded and does not block later agents.
- No API request, response, job-state, authentication, or GenStudio contract changes.
- The controller Hub remains outside the agent-Hub job and is updated last by GenStudio's existing maintenance coordinator.
- No new dependency, configuration, background service, or launcher change.
- Release `VERSION`, `CHANGELOG.md`, and frontend `RELEASE_NOTES` together.

---

### Task 1: Serialize the existing agent-Hub update job

**Files:**
- Modify: `app/backend/fleet_ops.py`
- Test: `app/tests/test_fleet_ops.py`
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Modify: `app/frontend/index.html`

**Interfaces:**
- Consumes: `_update_hub_one(item: dict, latest: str | None) -> Awaitable[None]` and the existing ordered `job["items"]` list.
- Produces: unchanged `start_hub_updates(...)` job payloads and unchanged durable terminal item/job states.

- [x] **Step 1: Write the failing serialization and failure-continuation test**

```python
@pytest.mark.asyncio
async def test_agent_hub_updates_run_canary_first_then_continue_after_failure(monkeypatch):
    active = 0
    max_active = 0
    order = []

    async def update_one(item, latest):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        order.append(item["machine"])
    try:
        await asyncio.sleep(0)
        if item["machine"] == "mac-a":
            raise RuntimeError("unexpected restart poll failure")
        item.update(status="complete", detail=f"healthy on v{latest}")
    finally:
        active -= 1

    monkeypatch.setattr(fleet_ops, "_update_hub_one", update_one)
    job = {
        "id": "rolling-1",
        "status": "queued",
        "latest": "2.6.2",
        "items": [
            {"machine": "mac-a", "status": "queued"},
            {"machine": "mac-b", "status": "queued"},
            {"machine": "mac-c", "status": "queued"},
        ],
    }

    await fleet_ops._run_hub_updates(job)

    assert order == ["mac-a", "mac-b", "mac-c"]
    assert max_active == 1
    assert job["items"][0]["status"] == "failed"
    assert "unexpected restart poll failure" in job["items"][0]["detail"]
    assert [item["status"] for item in job["items"][1:]] == ["complete", "complete"]
    assert job["status"] == "complete"
    assert job["degraded"] is True
```

- [x] **Step 2: Run the regression and verify the current concurrent implementation fails**

Run: `conda_env/bin/python -m pytest -q app/tests/test_fleet_ops.py::test_agent_hub_updates_run_canary_first_then_continue_after_failure`

Expected: FAIL because `asyncio.gather` overlaps `_update_hub_one` calls and `max_active` exceeds `1`.

- [x] **Step 3: Replace concurrent fan-out with ordered iteration**

```python
async def _run_hub_updates(job: dict):
    job["status"] = "running"
    _save_state()
    for item in job["items"]:
        try:
            await _update_hub_one(item, job.get("latest"))
        except Exception as exc:
            item.update(status="failed", detail=str(exc)[:240], finished_at=time.time())
        _save_state()
    finish_fleet_job(job)
    _save_state()
```

- [x] **Step 4: Run focused Hub update tests**

Run: `conda_env/bin/python -m pytest -q app/tests/test_fleet_ops.py app/tests/test_api.py`

Expected: PASS, including ordered execution, per-machine failure continuation, durable state, auth, and API compatibility.

- [x] **Step 5: Release the behavior as Studio Hub 2.6.2**

Set `VERSION` to `2.6.2`. Add a dated `2.6.2` section to `CHANGELOG.md` and the frontend `RELEASE_NOTES` list explaining that agent Hubs now update one at a time with the first agent acting as the canary, failures remain per-machine, and the GenStudio contract is unchanged.

- [x] **Step 6: Run the full release verification**

Run: `conda_env/bin/python -m pytest -q`

Run: `conda_env/bin/python -m compileall -q app`

Run: `for file in *.js; do node --check "$file"; done`

Run: `for file in *.sh; do bash -n "$file"; done`

Run: `git diff --check`

Expected: all checks pass and release metadata reports `2.6.2` dated `2026-08-11`.

- [ ] **Step 7: Commit and push the release**

```bash
git add app/backend/fleet_ops.py app/tests/test_fleet_ops.py VERSION CHANGELOG.md app/frontend/index.html docs/superpowers/plans/2026-08-11-agent-hub-rolling-updates.md
git commit -m "release: roll agent Hub updates safely"
git push origin main
```

- [ ] **Step 8: Prepare the Claude return handoff**

Write one sanitized handoff under `/Users/thengmacmini/Developer/_handoffs/` containing the Studio Hub version and commit, unchanged API/job contract, ordered canary-first behavior, per-machine failure continuation, controller-last ownership, and exact verification evidence. GenStudio requires no code change unless its tests incorrectly assume same-site concurrency.
