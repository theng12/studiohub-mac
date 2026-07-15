from backend import registry as reg


def test_default_launcher_folders_exist():
    for studio in reg.DEFAULT_STUDIOS:
        assert (reg.LAUNCHER_ROOT.parent / studio["app"]).is_dir(), studio["id"]


def test_default_registry_has_six_local_studios(reset):
    studios = reg.load_registry()
    ids = {s["id"] for s in studios}
    assert ids == {"image", "music", "voice", "chat", "video", "render"}
    assert all(s["machine"] == "local" for s in studios)
    assert all(s["host"] == "127.0.0.1" for s in studios)


def test_base_url():
    assert reg.base_url({"host": "1.2.3.4", "port": 47870}) == "http://1.2.3.4:47870"


def test_add_and_remove_machine(reset):
    entries = reg.build_machine_entries("100.1.1.1", "mac-b", ["image", "voice"])
    assert {e["id"] for e in entries} == {"image@mac-b", "voice@mac-b"}
    added = reg.add_user_entries(entries)
    assert added == 2
    assert reg.add_user_entries(entries) == 0  # idempotent, no dupes
    ids = {s["id"] for s in reg.load_registry()}
    assert {"image@mac-b", "voice@mac-b"} <= ids
    removed = reg.remove_machine("mac-b")
    assert removed == 2
    ids = {s["id"] for s in reg.load_registry()}
    assert "image@mac-b" not in ids


def test_duplicate_network_endpoint_is_not_registered_twice(reset):
    first = reg.build_machine_entries("100.1.1.1", "mac-b", ["image", "voice"])
    duplicate_alias = reg.build_machine_entries("100.1.1.1", "renamed-mac", ["image", "voice"])
    assert reg.add_user_entries(first) == 2
    assert reg.add_user_entries(duplicate_alias) == 0
    ids = {s["id"] for s in reg.load_registry()}
    assert "image@renamed-mac" not in ids and "voice@renamed-mac" not in ids


def test_remove_single_studio(reset):
    reg.add_user_entries(reg.build_machine_entries(
        "100.1.1.1", "mac-b", ["image", "music", "video"]))
    assert reg.remove_studio("music@mac-b") == 1        # prune one
    assert reg.remove_studio("music@mac-b") == 0        # already gone
    ids = {s["id"] for s in reg.load_registry()}
    assert "music@mac-b" not in ids
    assert {"image@mac-b", "video@mac-b"} <= ids        # siblings untouched


def test_remove_studio_endpoint_rejects_local(authed):
    # a default local studio isn't in studios.json → cannot be pruned
    assert authed.delete("/api/hub/registry/studios/image").status_code == 400


def test_build_machine_entries_skips_unknown_modality(reset):
    entries = reg.build_machine_entries("100.1.1.1", "x", ["image", "bogus"])
    assert {e["modality"] for e in entries} == {"image"}


def test_labels_roundtrip(reset):
    assert reg.label_for("local") == "local"  # default = key
    reg.set_label("local", "My MacBook")
    assert reg.label_for("local") == "My MacBook"
    reg.set_label("local", "")  # empty clears
    assert reg.label_for("local") == "local"


def test_user_entry_overrides_default(reset):
    reg.add_user_entries([{"id": "image", "host": "9.9.9.9", "machine": "remote"}])
    img = next(s for s in reg.load_registry() if s["id"] == "image")
    assert img["host"] == "9.9.9.9"  # override applied
    assert img["port"] == 47868      # default field preserved


def test_malformed_studios_json_falls_back(reset):
    reg.REGISTRY_FILE.write_text("{ not json")
    studios = reg.load_registry()  # must not raise
    assert len(studios) == 6


def test_registry_endpoint_rejects_url_shaped_host_and_unsafe_machine(authed):
    assert authed.post("/api/hub/registry/add", json={
        "host": "http://127.0.0.1/private", "machine": "mac-b",
    }).status_code == 400
    assert authed.post("/api/hub/registry/add", json={
        "host": "100.1.1.1", "machine": "mac@spoofed",
    }).status_code == 400
