import json


def test_hf_token_metadata_never_contains_secret(reset, monkeypatch):
    from backend import hf_credentials

    saved = {}
    monkeypatch.setattr(hf_credentials, "_keychain_write", lambda token: saved.setdefault("token", token))
    monkeypatch.setattr(hf_credentials, "_keychain_read", lambda: saved.get("token"))
    public = hf_credentials.save_token("hf_test_secret")
    assert public["configured"] is True
    assert public["credential_id"].startswith("hfcred_")
    serialized = hf_credentials.STATE_FILE.read_text()
    assert "hf_test_secret" not in serialized
    assert "token" not in serialized
    assert json.loads(serialized)["credential_id"] == public["credential_id"]


def test_delivery_state_is_retryable_without_secret(reset, monkeypatch):
    from backend import hf_credentials

    monkeypatch.setattr(hf_credentials, "_keychain_read", lambda: "hf_test_secret")
    hf_credentials.record_delivery({
        "voice@mac-b": {"ok": False, "status": 503, "error": "peer unavailable"},
        "image@mac-c": {"ok": True, "status": 200},
    })
    state = hf_credentials.status()
    assert state["pending_count"] == 1
    assert state["deliveries"]["voice@mac-b"]["status"] == "retryable"
    assert state["deliveries"]["image@mac-c"]["status"] == "delivered"
    assert "hf_test_secret" not in hf_credentials.STATE_FILE.read_text()


def test_hf_credential_endpoints_save_and_retry(authed, monkeypatch):
    from backend import hf_credentials, main

    async def valid(_token):
        return {"name": "owner"}

    async def broadcast_saved(studios):
        return {"credential": {"configured": True},
                "results": {s["id"]: {"ok": True} for s in studios}}

    monkeypatch.setattr(main, "_validate_hf_token", valid)
    monkeypatch.setattr(hf_credentials, "save_token", lambda _token: {"configured": True})
    monkeypatch.setattr(main, "_broadcast_saved_hf_token", broadcast_saved)
    saved = authed.post("/api/hub/credentials/huggingface", json={"token": "hf_new"})
    assert saved.status_code == 200
    assert "hf_new" not in saved.text

    monkeypatch.setattr(hf_credentials, "get_token", lambda: "hf_new")
    retried = authed.post("/api/hub/credentials/huggingface/retry", json={})
    assert retried.status_code == 200
    assert "hf_new" not in retried.text
