import base64

import httpx
import pytest

from backend import broker, ledger


def test_submit_validation(reset):
    assert "error" in broker.submit_batch({"modality": "bogus", "items": [{}], "model": "m"})
    assert "error" in broker.submit_batch({"modality": "image", "items": [], "model": "m"})
    assert "error" in broker.submit_batch({"modality": "image", "items": [{"prompt": "x"}]})  # no model


def test_submit_ok_and_summary(reset):
    r = broker.submit_batch({"modality": "image", "model": "a/b", "label": "t",
                             "items": [{"prompt": "one"}, {"prompt": "two"}]})
    assert r["items"] == 2
    b = broker.batches[r["batch_id"]]
    s = broker.batch_summary(b)
    assert s["total"] == 2 and s["queued"] == 2 and s["label"] == "t"


def test_summary_running_items_and_avg(reset):
    import time
    r = broker.submit_batch({"modality": "image", "model": "a/b",
                             "items": [{"prompt": "one"}, {"prompt": "two"}]})
    b = broker.batches[r["batch_id"]]
    # item 0 running on a remote machine with live progress; item 1 done+timed
    b["items"][0].update(state="running", studio="image@macmini-m1-01",
                         run_started=time.time() - 10, progress=0.5)
    b["items"][1].update(state="done", duration_s=8.0)
    s = broker.batch_summary(b)
    assert s["avg_s"] == 8.0
    assert len(s["running_items"]) == 1
    ri = s["running_items"][0]
    assert ri["machine"] == "macmini-m1-01" and ri["progress"] == 0.5
    assert ri["elapsed_s"] >= 9   # ~10s elapsed


def test_summary_local_running_item_machine(reset):
    import time
    r = broker.submit_batch({"modality": "image", "model": "a/b",
                             "items": [{"prompt": "x"}]})
    b = broker.batches[r["batch_id"]]
    b["items"][0].update(state="running", studio="image",
                         run_started=time.time(), progress=None)
    ri = broker.batch_summary(b)["running_items"][0]
    assert ri["machine"] == "local" and ri["progress"] is None


def test_prompt_and_text_both_accepted(reset):
    r = broker.submit_batch({"modality": "voice", "model": "a/b",
                             "items": [{"text": "spoken"}]})
    b = broker.batches[r["batch_id"]]
    assert b["items"][0]["prompt"] == "spoken"


def test_cancel_batch(reset):
    r = broker.submit_batch({"modality": "image", "model": "a/b",
                             "items": [{"prompt": "x"}]})
    assert broker.cancel_batch(r["batch_id"]) is True
    b = broker.batches[r["batch_id"]]
    assert b["cancelled"] and b["items"][0]["state"] == "cancelled"
    assert broker.cancel_batch("nope") is False


def test_multipart_fields():
    out = broker._multipart_fields({"repo": "a/b", "prompt": "hi", "width": 512,
                                    "seed": None, "lora_names": ["x", "y"],
                                    "lora_scales": [0.5, 1.0], "ignored": "z"})
    assert out["repo"] == "a/b" and out["width"] == "512"
    assert out["lora_names"] == "x,y" and out["lora_scales"] == "0.5,1.0"
    assert "seed" not in out and "ignored" not in out


@pytest.mark.asyncio
async def test_resolve_reference_b64():
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    ref = {"b64": base64.b64encode(png).decode(), "mime": "image/png"}
    async with httpx.AsyncClient() as c:
        data, mime = await broker._resolve_reference(c, ref)
    assert data == png and mime == "image/png"


@pytest.mark.asyncio
async def test_resolve_reference_data_url_prefix():
    raw = base64.b64encode(b"hello").decode()
    ref = {"b64": f"data:image/png;base64,{raw}"}
    async with httpx.AsyncClient() as c:
        data, _ = await broker._resolve_reference(c, ref)
    assert data == b"hello"


@pytest.mark.asyncio
async def test_resolve_reference_asset_id(reset, tmp_path):
    p = tmp_path / "ref.png"
    p.write_bytes(b"IMGBYTES")
    aid = ledger.record_asset(source="upload", modality="image", machine="local",
                              artifact_path=str(p))
    async with httpx.AsyncClient() as c:
        data, _ = await broker._resolve_reference(c, {"asset_id": aid})
    assert data == b"IMGBYTES"


@pytest.mark.asyncio
async def test_resolve_reference_errors(reset):
    async with httpx.AsyncClient() as c:
        with pytest.raises(ValueError):
            await broker._resolve_reference(c, {})
        with pytest.raises(ValueError):
            await broker._resolve_reference(c, {"asset_id": "missing"})


def test_local_gate_skip_when_machine_too_small(reset):
    # BUG FIX: a too-small LOCAL machine must SKIP (so a bigger remote can take
    # the job), never error the whole batch.
    mem = {"min_total": 32, "size": 10}
    host = {"total_gb": 16, "available_gb": 12}
    decision, note = broker._local_gate(mem, host)
    assert decision == "skip" and "32GB" in note


def test_local_gate_run_when_fits(reset):
    decision, _ = broker._local_gate({"min_total": 8, "size": 2},
                                     {"total_gb": 16, "available_gb": 10})
    assert decision == "run"


def test_local_gate_wait_when_not_enough_free(reset):
    decision, _ = broker._local_gate({"min_total": 8, "size": 10},
                                     {"total_gb": 16, "available_gb": 5})
    assert decision == "wait"


def test_local_gate_reservation_prevents_double_load(reset):
    mem = {"min_total": 8, "size": 6}
    host = {"total_gb": 16, "available_gb": 10}  # 10 free, model needs 6+1=7
    assert broker._local_gate(mem, host)[0] == "run"
    # simulate one in-flight local dispatch reserving 6GB
    broker._reserved["gb"] = 6.0
    # now only ~4GB effectively free -> the second must WAIT, not double-load
    assert broker._local_gate(mem, host)[0] == "wait"


def test_restore_batches_requeues_running(reset):
    b = {"id": "bx", "modality": "image", "model": "a/b", "created_at": 1.0,
         "cancelled": False, "shared_params": {}, "routing": "pool",
         "items": [{"index": 0, "state": "running", "studio": "image",
                    "studio_job_id": "j1", "prompt": "x", "seed": None,
                    "params": {}, "tries": 1, "artifact_path": None,
                    "artifact_url": None, "asset_id": None, "error": None}]}
    ledger.save_batch(b)
    n = broker.restore_batches()
    assert n == 1
    # the in-flight item must be re-queued (its studio job is orphaned)
    assert broker.batches["bx"]["items"][0]["state"] == "queued"
    assert broker.batches["bx"]["items"][0]["studio"] is None
