from backend import registry as reg


def test_default_launcher_folders_exist():
    for studio in reg.DEFAULT_STUDIOS:
        assert (reg.LAUNCHER_ROOT.parent / studio["app"]).is_dir(), studio["id"]


def test_default_registry_has_five_local_studios(reset):
    studios = reg.load_registry()
    ids = {s["id"] for s in studios}
    assert ids == {"image", "music", "voice", "chat", "video"}
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
    assert len(studios) == 5
