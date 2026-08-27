import json

import pytest

from backend import registry as reg


_SIBLING_CHECKOUTS_PRESENT = all(
    (reg.LAUNCHER_ROOT.parent / studio["app"]).is_dir()
    for studio in reg.DEFAULT_STUDIOS
)


@pytest.mark.skipif(
    not _SIBLING_CHECKOUTS_PRESENT,
    reason="Pinokio sibling checkouts are not part of the standalone CI repository",
)
def test_default_launcher_folders_exist():
    for studio in reg.DEFAULT_STUDIOS:
        assert (reg.LAUNCHER_ROOT.parent / studio["app"]).is_dir(), studio["id"]


def test_default_registry_tracks_only_image_and_voice(reset):
    studios = reg.load_registry()
    ids = {s["id"] for s in studios}
    assert ids == {"image", "voice"}
    assert all(s["machine"] == "local" for s in studios)
    assert all(s["host"] == "127.0.0.1" for s in studios)


def test_missing_local_checkout_without_removal_marker_keeps_default(reset, tmp_path, monkeypatch):
    launcher = tmp_path / "api" / "studiohub-mac"
    launcher.mkdir(parents=True)
    monkeypatch.setattr(reg, "LAUNCHER_ROOT", launcher)

    ids = {studio["id"] for studio in reg.load_registry()}

    assert ids == {"image", "voice"}


def test_saved_legacy_studios_are_not_tracked(reset):
    reg.REGISTRY_FILE.write_text(json.dumps([
        {"id": "image@mac-b", "modality": "image", "host": "100.1.1.1",
         "port": 47868, "machine": "mac-b"},
        {"id": "chat@mac-b", "modality": "chat", "host": "100.1.1.1",
         "port": 47871, "machine": "mac-b"},
        {"id": "render@mac-b", "modality": "render", "host": "100.1.1.1",
         "port": 47874, "machine": "mac-b"},
    ]))

    ids = {studio["id"] for studio in reg.load_registry()}

    assert ids == {"image", "voice", "image@mac-b"}


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


def test_reenrolling_same_machine_updates_its_address(reset):
    original = reg.build_machine_entries("100.1.1.1", "mac-b", ["image", "voice"])
    moved = reg.build_machine_entries("100.1.1.2", "mac-b", ["image", "voice"])

    assert reg.add_user_entries(original) == 2
    assert reg.add_user_entries(moved) == 0
    remote = [row for row in reg.load_registry() if row["machine"] == "mac-b"]
    assert {row["host"] for row in remote} == {"100.1.1.2"}


def test_build_machine_entries_skips_untracked_studios(reset):
    entries = reg.build_machine_entries(
        "100.1.1.1", "mac-b", ["image", "music", "voice", "chat", "video", "render"])

    assert {entry["modality"] for entry in entries} == {"image", "voice"}


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


def test_per_studio_scheduler_flags_are_independent_and_persistent(reset):
    assert reg.studio_enabled("local", "image") is True
    assert reg.studio_enabled("local", "voice") is True

    reg.set_studio_enabled("local", "image", False)
    assert reg.studio_enabled("local", "image") is False
    assert reg.studio_enabled("local", "voice") is True

    reg._flags_cache = None  # prove the value survives a Hub process reload
    assert reg.studio_enabled("local", "image") is False
    reg.set_studio_enabled("local", "image", True)
    assert reg.studio_enabled("local", "image") is True


def test_studio_scheduler_and_removal_flags_preserve_each_other(reset):
    reg.set_studio_removed("local", "music", True)
    reg.set_studio_enabled("local", "music", False)

    assert reg.studio_removed("local", "music") is True
    assert reg.studio_enabled("local", "music") is False

    reg.set_studio_removed("local", "music", False)
    assert reg.studio_removed("local", "music") is False
    assert reg.studio_removal_complete("local", "music") is False
    assert reg.studio_enabled("local", "music") is False


def test_removal_completion_is_separate_from_pre_cleanup_intent(reset):
    reg.set_studio_removed("local", "music", True)
    assert reg.studio_removed("local", "music") is True
    assert reg.studio_removal_complete("local", "music") is False

    reg.set_studio_removal_complete("local", "music", True)
    assert reg.studio_removal_complete("local", "music") is True

    reg.set_studio_removed("local", "music", False)
    assert reg.studio_removed("local", "music") is False
    assert reg.studio_removal_complete("local", "music") is False


def test_remove_studio_clears_its_scheduler_flag(reset):
    reg.add_user_entries(reg.build_machine_entries(
        "100.1.1.1", "mac-b", ["image", "voice"]))
    reg.set_studio_enabled("mac-b", "image@mac-b", False)

    assert reg.remove_studio("image@mac-b") == 1
    assert "image@mac-b" not in reg.load_flags()["mac-b"]["studios"]
    assert reg.studio_enabled("mac-b", "voice@mac-b") is True


def test_user_entry_overrides_default(reset):
    reg.add_user_entries([{"id": "image", "host": "9.9.9.9", "machine": "remote"}])
    img = next(s for s in reg.load_registry() if s["id"] == "image")
    assert img["host"] == "9.9.9.9"  # override applied
    assert img["port"] == 47868      # default field preserved


def test_malformed_studios_json_falls_back(reset):
    reg.REGISTRY_FILE.write_text("{ not json")
    studios = reg.load_registry()  # must not raise
    assert len(studios) == 2


def test_registry_endpoint_rejects_url_shaped_host_and_unsafe_machine(authed):
    assert authed.post("/api/hub/registry/add", json={
        "host": "http://127.0.0.1/private", "machine": "mac-b",
    }).status_code == 400
    assert authed.post("/api/hub/registry/add", json={
        "host": "100.1.1.1", "machine": "mac@spoofed",
    }).status_code == 400


def test_repair_snapshot_is_read_only_and_requires_exact_private_address(reset):
    reg.add_user_entries(reg.build_machine_entries("agent.test", "mac-a", ["image", "voice"]))
    before = reg.REGISTRY_FILE.read_bytes()

    snapshot = reg.repair_machine_snapshot(
        reg.load_registry(), "mac-a",
        resolver=lambda host, port, *, type: [(2, type, 6, "", ("100.64.0.10", port))],
    )

    assert snapshot.machine == "mac-a"
    assert snapshot.registry_host == "agent.test"
    assert snapshot.resolved_address == "100.64.0.10"
    assert snapshot.endpoint_ids == ("image@mac-a", "voice@mac-a")
    assert reg.REGISTRY_FILE.read_bytes() == before


def test_repair_snapshot_rejects_distinct_hosts_with_one_shared_address(reset):
    rows = [
        {"id": "image@mac-a", "machine": "mac-a", "host": "a.test", "port": 47868},
        {"id": "voice@mac-b", "machine": "mac-b", "host": "b.test", "port": 47870},
    ]

    with pytest.raises(reg.RepairRegistryAmbiguity, match="address_shared"):
        reg.repair_machine_snapshot(
            rows, "mac-a",
            resolver=lambda host, port, *, type: [(2, type, 6, "", ("100.64.0.10", port))],
        )
