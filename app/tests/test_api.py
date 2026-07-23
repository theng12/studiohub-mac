from pathlib import Path


def test_dashboard_includes_render_studio():
    dashboard = (Path(__file__).parents[1] / "frontend" / "index.html").read_text()
    assert '["image", "chat", "voice", "music", "video", "render"]' in dashboard
    assert 'class="d-mod" value="render"' in dashboard
    assert '<option value="render">Render</option>' in dashboard
    assert 'class="workspace-head"' in dashboard
    assert 'const TAB_META = {' in dashboard
    assert 'id="res-machine-sort"' in dashboard
    assert 'localStorage.getItem("res_machine_sort") || "status"' in dashboard
    assert 'localStorage.getItem("res_sort") || "status"' in dashboard
    assert 'class="resource-studio-table"' in dashboard
    assert '<col style="width:32%"><col style="width:23%">' in dashboard
    assert 'id="a-sort"' in dashboard
    assert 'localStorage.getItem("asset_sort") || "newest"' in dashboard
    assert '>Working</button>' in dashboard
    assert 'function stState(s) { return s.busy ? "generating"' in dashboard
    assert 'return compact ? "LLM" : "LLM working"' in dashboard
    assert 'Priority #${rank}' in dashboard
    assert 'loadActiveJobQueues();' in dashboard
    assert 'if (vis("jobs")) renderBatches(sum.jobs);' in dashboard
    assert 'const JOB_QUEUE_REFRESH_MS = 3000;' in dashboard
    assert 'if (vis("jobs") && !document.hidden) loadActiveJobQueues();' in dashboard
    assert 'document.addEventListener("visibilitychange"' in dashboard
    assert 'id="fleet-save"' in dashboard
    assert 'id="fleet-save-result" role="status" aria-live="polite"' in dashboard
    assert 'JSON.stringify({ token, sync: true })' in dashboard
    assert 'id="su-rescan"' in dashboard
    assert 'id="su-progress" class="update-progress hide" role="status"' in dashboard
    assert 'id="hubupd-status" class="update-progress hide" role="status"' in dashboard
    assert 'id="hubupd-sort"' in dashboard
    assert 'id="hubupd-sort-dir"' in dashboard
    assert 'localStorage.getItem("hub_machine_sort") || "status"' in dashboard
    assert 'function _hubHardware(machine, row = {})' in dashboard
    assert '<th>Machine</th><th>Chip</th><th>RAM</th>' in dashboard
    assert 'onclick="updateReadyHubs()"' in dashboard
    assert 'function startHubUpdate(machines = null)' in dashboard
    assert 'class="btn primary compact"' in dashboard
    assert 'function providerHealthHTML(s, compact = false)' in dashboard
    assert 'sum.cloud_providers?.ready_count || 0' in dashboard
    assert '>Cancel image queue</button>' in dashboard
    assert 'data-job-kind="image"' in dashboard
    assert 'data-job-kind="voice"' in dashboard
    assert 'data-job-kind="transcription"' in dashboard
    assert 'data-job-kind="chat"' in dashboard
    assert 'per: 10' in dashboard
    assert 'generationDetailToggle(this' in dashboard
    assert 'function toggleStudio(id, enabled)' in dashboard
    assert 'new jobs for only that app' in dashboard


def test_job_storage_cap_defaults_to_safe_fleet_policy_and_is_configurable(authed):
    initial = authed.get("/api/hub/job-storage")
    assert initial.status_code == 200
    assert initial.json()["enabled"] is True
    assert initial.json()["max_bytes"] == 80 * 1024 ** 3
    assert initial.json()["retention_days"] == 30
    saved = authed.post("/api/hub/job-storage", json={"enabled": True, "max_gb": 5})
    assert saved.status_code == 200
    assert saved.json()["enabled"] is True
    assert saved.json()["max_bytes"] == 5 * 1024 ** 3


def test_health_and_version(client):
    h = client.get("/api/health").json()
    assert h["ok"] is True and "app_version" in h
    v = client.get("/api/version").json()
    assert v["title"] == "Studio Hub KH"


def test_reported_version_is_snapshot_of_loaded_process(tmp_path, monkeypatch):
    from backend import main

    monkeypatch.setattr(main, "LAUNCHER_ROOT", tmp_path)
    (tmp_path / "VERSION").write_text("99.0.0")
    assert main._read_app_version() == "99.0.0"
    assert main._app_version() == main.APP_VERSION
    assert main._app_version() != "99.0.0"


def test_hub_health_and_studios(authed):
    hh = authed.get("/api/hub/health").json()
    assert hh["studios_total"] == 6 and hh["studios_up"] == 0
    studios = authed.get("/api/hub/studios").json()["studios"]
    assert len(studios) == 6
    assert all("machine_label" in s for s in studios)


def test_cloud_provider_health_is_aggregated_without_keys(authed):
    import time
    from backend import main

    main.monitor.status["voice"] = {"status": "up", "last_seen": time.time()}
    main.monitor._provider_cache["voice"] = (time.time(), {
        "supported": True,
        "providers": [{
            "key": "genaipro", "name": "GenAIPro", "has_key": True,
            "paid": True, "enabled": True, "live": True, "models": 4,
        }, {
            "key": "fal", "name": "fal.ai", "has_key": True,
            "paid": False, "enabled": True, "live": False, "models": 0,
        }],
    })

    response = authed.get("/api/hub/providers")
    assert response.status_code == 200
    data = response.json()
    assert data["provider_count"] == 2
    assert data["ready_count"] == 1
    genaipro = next(row for row in data["providers"] if row["key"] == "genaipro")
    assert genaipro["ready_on"] == ["local"]
    assert genaipro["endpoints"][0]["models"] == 4
    assert "api_key" not in response.text

    summary = authed.get("/api/hub/summary").json()
    voice = next(row for row in summary["studios"] if row["id"] == "voice")
    assert voice["cloud_providers"]["providers"][0]["key"] == "genaipro"
    assert summary["cloud_providers"]["ready_count"] == 1


def test_render_asset_stream_round_trip(authed):
    payload = b"render-input" * 100
    checksum = __import__("hashlib").sha256(payload).hexdigest()
    uploaded = authed.post(
        "/api/hub/render-assets", content=payload,
        headers={"X-File-Name": "scene.mp4", "X-Content-SHA256": checksum})
    assert uploaded.status_code == 200
    result = uploaded.json()
    assert result["bytes"] == len(payload) and result["sha256"] == checksum
    downloaded = authed.get(result["path"])
    assert downloaded.content == payload
    retained = authed.get(f"/api/hub/render-assets/by-sha/{checksum}?extension=.mp4")
    assert retained.status_code == 200
    assert retained.json()["asset_id"] == result["asset_id"]
    # Uploading the same immutable bytes is a no-op instead of a second file.
    duplicate = authed.post(
        "/api/hub/render-assets", content=payload,
        headers={"X-File-Name": "scene.mp4", "X-Content-SHA256": checksum})
    assert duplicate.status_code == 200
    assert duplicate.json()["asset_id"] == result["asset_id"]
    assert authed.delete(result["path"]).status_code == 409


def test_render_asset_rejects_unsafe_type(authed):
    response = authed.post(
        "/api/hub/render-assets", content=b"bad",
        headers={"X-File-Name": "script.sh"})
    assert response.status_code == 415


def test_update_status(authed):
    d = authed.get("/api/update-status").json()
    assert "app_version" in d and "update_available" in d


def test_stats_empty(authed):
    d = authed.get("/api/hub/stats").json()
    assert d["total"] == 0 and d["by_machine"] == {}
    # lane facet is always present (both lanes reported even when empty)
    assert d["by_lane"] == {"local": 0, "cloud": 0}
    assert d["filters"]["lane"] == "all"


def test_models_empty_when_all_down(authed):
    d = authed.get("/api/hub/models").json()
    assert d["count"] == 0
    # lane/provider summaries are always present so the UI can group by lane
    assert d["lanes"] == {"local": 0, "cloud": 0}
    assert d["providers"] == {}


def test_transcription_empty_when_all_voice_studios_down(authed):
    d = authed.get("/api/hub/transcription").json()
    assert d["available"] is False
    assert d["models"] == []
    assert d["endpoint_count"] == 0


def test_transcription_gateway_routes_with_studio_auth(authed, monkeypatch):
    import time
    from backend import main, peers

    main.monitor.status["voice"] = {"status": "up", "last_seen": time.time()}
    main.monitor._transcribe_cache["voice"] = (time.time(), {
        "available": True,
        "models": [{"repo": "mlx/whisper", "cached": True}],
    })
    captured = {}

    class Response:
        status_code = 200
        def json(self):
            return {"srt": "1\n00:00:00,000 --> 00:00:01,000\nHello\n"}

    async def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(main.monitor._client, "post", post)
    response = authed.post(
        "/api/hub/transcribe",
        data={"model": "mlx/whisper", "language": "en"},
        files={"file": ("clip.wav", b"audio", "audio/wav")},
    )
    assert response.status_code == 200
    assert captured["url"].endswith("/api/transcribe")
    voice = next(s for s in main.monitor.registry if s["id"] == "voice")
    assert captured["headers"] == peers.studio_headers(voice)
    assert main._transcription_busy == set()


def test_fleet_get_set(authed):
    assert authed.get("/api/hub/fleet").json()["fleet_token_set"] is True
    response = authed.post("/api/hub/fleet", json={"token": "a-valid-fleet-token"})
    assert response.json()["ok"] is True
    assert authed.get("/api/hub/fleet").json()["fleet_token_set"] is True
    from backend import peers
    import stat
    assert stat.S_IMODE(peers.FLEET_TOKEN_FILE.stat().st_mode) == 0o600


def test_owner_session_can_reveal_tokens_but_machine_token_cannot(app, token):
    from starlette.testclient import TestClient
    from backend import auth, peers

    peers.set_fleet_token("owner-fleet-secret")
    machine = TestClient(app, client=("100.66.3.3", 50000),
                         headers={"X-Hub-Token": token})
    assert "token" not in machine.get("/api/hub/access").json()
    assert "token" not in machine.get("/api/hub/fleet").json()

    owner = TestClient(app, client=("100.66.3.3", 50000))
    owner.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_browser_session())
    assert owner.get("/api/hub/access").json()["token"] == token
    assert owner.get("/api/hub/fleet").json()["token"] == "owner-fleet-secret"


def test_fleet_save_rejects_ambiguous_short_credentials(authed):
    response = authed.post("/api/hub/fleet", json={"token": "short"})
    assert response.status_code == 400


def test_update_route_schedules_on_event_loop(authed, monkeypatch):
    from backend import fleet_ops

    async def finish(mon, job):
        job["status"] = "complete"
        job["finished_at"] = 1

    monkeypatch.setattr(fleet_ops, "_run_updates", finish)
    response = authed.post("/api/hub/maintenance/updates", json={"studio_ids": ["image"]})
    assert response.status_code == 200
    assert response.json()["id"] in fleet_ops._updates


def test_asset_upload_limits_and_types(authed, monkeypatch):
    from backend import main
    ok = authed.post("/api/hub/assets/upload", files={"file": ("ref.png", b"png", "image/png")})
    assert ok.status_code == 200 and ok.json()["bytes"] == 3
    bad = authed.post("/api/hub/assets/upload", files={"file": ("ref.svg", b"<svg/>", "image/svg+xml")})
    assert bad.status_code == 415
    monkeypatch.setattr(main, "_MAX_IMAGE_UPLOAD_BYTES", 2)
    large = authed.post("/api/hub/assets/upload", files={"file": ("large.png", b"123", "image/png")})
    assert large.status_code == 413


def test_registry_add_rename_remove(authed):
    r = authed.post("/api/hub/registry/add",
                    json={"host": "100.9.9.9", "machine": "mac-z",
                          "modalities": ["image", "voice"]})
    assert r.json()["registered"] == 2
    studios = authed.get("/api/hub/studios").json()["studios"]
    assert any(s["id"] == "image@mac-z" for s in studios)
    # rename (label alias) — key stays, label changes
    authed.post("/api/hub/registry/machines/mac-z/name", json={"name": "Zeta"})
    studios = authed.get("/api/hub/studios").json()["studios"]
    z = next(s for s in studios if s["machine"] == "mac-z")
    assert z["machine_label"] == "Zeta" and z["id"] == "image@mac-z"
    # the same encoded id used by the dashboard controls a remote app only
    paused = authed.post(
        "/api/hub/registry/studios/image%40mac-z/enabled", json={"enabled": False})
    assert paused.status_code == 200 and paused.json()["studio"] == "image@mac-z"
    studios = authed.get("/api/hub/studios").json()["studios"]
    assert next(s for s in studios if s["id"] == "image@mac-z")["enabled"] is False
    assert next(s for s in studios if s["id"] == "voice@mac-z")["enabled"] is True
    # remove
    assert authed.request("DELETE", "/api/hub/registry/machines/mac-z").json()["removed"] == 2


def test_studio_scheduler_toggle_is_reported_without_interrupting_work(authed):
    from backend import broker, registry as reg

    broker._busy.add("image")
    response = authed.post(
        "/api/hub/registry/studios/image/enabled", json={"enabled": False})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "studio": "image", "machine": "local", "enabled": False,
    }
    image = next(row for row in authed.get("/api/hub/studios").json()["studios"]
                 if row["id"] == "image")
    assert image["enabled"] is False
    assert image["machine_enabled"] is True
    assert "image" in broker._busy  # scheduler pause never cancels active work
    assert reg.studio_enabled("local", "image") is False


def test_studio_scheduler_toggle_validates_target_and_boolean(authed):
    assert authed.post(
        "/api/hub/registry/studios/missing/enabled", json={"enabled": False}
    ).status_code == 404
    assert authed.post(
        "/api/hub/registry/studios/image/enabled", json={"enabled": "false"}
    ).status_code == 400


def test_remove_machine_purges_live_inventory_and_update_state(authed):
    import time
    from backend import fleet_ops, peers, registry as reg
    from backend.main import monitor

    authed.post("/api/hub/registry/add",
                json={"host": "100.9.9.8", "machine": "mac-clean",
                      "modalities": ["image", "voice"]})
    studio_ids = {"image@mac-clean", "voice@mac-clean"}
    reg.set_label("mac-clean", "Cleanup Mac")
    reg.set_machine_enabled("mac-clean", False)
    reg.set_studio_enabled("mac-clean", "image@mac-clean", False)
    for studio_id in studio_ids:
        monitor._catalog_cache[studio_id] = (time.time(), {"models": []})
        monitor._provider_cache[studio_id] = (time.time(), {"providers": []})
    peers._cache["mac-clean"] = (time.time(), {"reachable": True})
    fleet_ops._studio_versions = {
        "checked_at": time.time(),
        "studios": [{"id": studio_id, "machine": "mac-clean"}
                    for studio_id in studio_ids],
    }
    fleet_ops._hub_versions["mac-clean"] = {"version": "1.0.0"}

    response = authed.delete("/api/hub/registry/machines/mac-clean")

    assert response.status_code == 200
    assert not studio_ids.intersection({row["id"] for row in monitor.registry})
    assert not studio_ids.intersection(monitor.status)
    assert not studio_ids.intersection(monitor._catalog_cache)
    assert not studio_ids.intersection(monitor._provider_cache)
    assert "mac-clean" not in peers._cache
    assert "mac-clean" not in reg.load_labels()
    assert "mac-clean" not in reg.load_flags()
    assert fleet_ops._studio_versions["studios"] == []
    assert "mac-clean" not in fleet_ops._hub_versions


def test_cannot_remove_local(authed):
    assert authed.request("DELETE", "/api/hub/registry/machines/local").status_code == 400


def test_jobs_submit_list_get_cancel(authed):
    r = authed.post("/api/hub/jobs", json={"modality": "image", "model": "a/b",
                                           "items": [{"prompt": "x"}]})
    bid = r.json()["batch_id"]
    assert any(b["id"] == bid for b in authed.get("/api/hub/jobs").json()["batches"])
    got = authed.get(f"/api/hub/jobs/{bid}").json()
    assert got["total"] == 1 and got["items"][0]["prompt"] == "x"
    cancelled = authed.request("DELETE", f"/api/hub/jobs/{bid}").json()
    assert cancelled["ok"] is True and cancelled["queued_cancelled"] == 1
    cleared = authed.post(f"/api/hub/jobs/{bid}/clear").json()
    assert cleared["ok"] is True and cleared["cleared"] == 1
    assert authed.get(f"/api/hub/jobs/{bid}").status_code == 404
    assert authed.get("/api/hub/jobs/does-not-exist").status_code == 404


def test_bulk_image_cancel_and_clear_do_not_touch_other_modalities(authed):
    image = authed.post("/api/hub/jobs", json={
        "modality": "image", "model": "a/b", "items": [{"prompt": "image"}],
    }).json()["batch_id"]
    voice = authed.post("/api/hub/jobs", json={
        "modality": "voice", "model": "c/d", "items": [{"text": "voice"}],
    }).json()["batch_id"]

    cancelled = authed.post("/api/hub/jobs/cancel", json={"modality": "image"}).json()
    assert cancelled["batches_cancelled"] == 1
    assert authed.get(f"/api/hub/jobs/{image}").json()["cancelled"] is True
    assert authed.get(f"/api/hub/jobs/{voice}").json()["queued"] == 1

    cleared = authed.post("/api/hub/jobs/clear", json={"modality": "image"}).json()
    assert cleared["cleared"] == 1
    assert authed.get(f"/api/hub/jobs/{image}").status_code == 404
    assert authed.get(f"/api/hub/jobs/{voice}").status_code == 200


def test_jobs_bad_modality_400(authed):
    r = authed.post("/api/hub/jobs", json={"modality": "nope", "model": "a/b",
                                           "items": [{"prompt": "x"}]})
    assert r.status_code == 400


def test_watchdog_toggle(authed):
    r = authed.post("/api/hub/studios/image/watchdog", json={"enabled": True})
    assert r.json()["watchdog"]["enabled"] is True
    assert authed.post("/api/hub/studios/bogus/watchdog", json={"enabled": True}).status_code == 404


def test_update_status_never_calls_pulled_code_loaded(monkeypatch, authed):
    from backend import main

    monkeypatch.setattr(main.auto_updater, "public_status", lambda: {
        "installed_version": "9.9.9", "state": "succeeded",
        "last_update_result": "Updated successfully",
    })
    response = authed.get("/api/auto-update/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["installed_version"] == main.APP_VERSION
    assert payload["loaded_version"] == main.APP_VERSION
    assert payload["disk_version"] == "9.9.9"
    assert payload["state"] == "restart_required"
    assert payload["restart_required"] is True


def test_fleet_update_inventory_uses_loaded_hub_version(monkeypatch, authed):
    from backend import main

    async def snapshot():
        return {"apps": [{"id": "hub@local", "kind": "hub",
                           "installed_version": "9.9.9", "state": "succeeded"}]}

    monkeypatch.setattr(main.fleet_auto_updates, "snapshot", snapshot)
    monkeypatch.setattr(main.auto_updater, "public_status", lambda: {
        "installed_version": "9.9.9", "state": "succeeded",
    })
    payload = authed.get("/api/hub/auto-updates").json()
    assert payload["apps"][0]["installed_version"] == main.APP_VERSION
    assert payload["apps"][0]["disk_version"] == "9.9.9"
    assert payload["apps"][0]["state"] == "restart_required"
