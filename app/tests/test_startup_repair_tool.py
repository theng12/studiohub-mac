import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load_tool():
    path = ROOT / "tools/repair_startup.py"
    spec = importlib.util.spec_from_file_location("repair_startup", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_app(home: Path, name: str, *, service: bool = False) -> Path:
    app = home / "api" / name
    app.mkdir(parents=True)
    (app / "ENVIRONMENT").write_text(
        "PINOKIO_SCRIPT_AUTOLAUNCH=old.js\n"
        "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=true\n"
        "PINOKIO_SCRIPT_REQUIRES=imagestudio-mac,voicestudio-mac\n"
    )
    if service:
        marker = app / "service/.installed"
        marker.parent.mkdir()
        marker.write_text("installed\n")
    return app


def test_repair_makes_each_app_independent_and_service_owned_apps_non_autolaunch(tmp_path):
    repair = load_tool()
    image = make_app(tmp_path, "imagestudio-mac", service=True)
    voice = make_app(tmp_path, "voicestudio-mac")
    hub = make_app(tmp_path, "studiohub-mac")
    duplicate = make_app(tmp_path, "voicestudio-mac.git")

    result = repair.repair_startup(tmp_path, dry_run=False)
    repair.repair_startup(tmp_path, dry_run=False)

    assert result == 0
    assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=false" in (image / "ENVIRONMENT").read_text()
    assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=true" in (voice / "ENVIRONMENT").read_text()
    assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=true" in (hub / "ENVIRONMENT").read_text()
    assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=false" in (duplicate / "ENVIRONMENT").read_text()
    for app in (image, voice, hub, duplicate):
        value = (app / "ENVIRONMENT").read_text()
        assert "PINOKIO_SCRIPT_REQUIRES=" in value
        assert "imagestudio-mac,voicestudio-mac" not in value
        assert value.count("PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=") == 1


def test_repair_dry_run_writes_nothing(tmp_path):
    repair = load_tool()
    hub = make_app(tmp_path, "studiohub-mac")
    before = (hub / "ENVIRONMENT").read_bytes()

    assert repair.repair_startup(tmp_path, dry_run=True) == 0
    assert (hub / "ENVIRONMENT").read_bytes() == before


def test_legacy_service_owner_wins_over_canonical_duplicate(tmp_path):
    repair = load_tool()
    canonical = make_app(tmp_path, "voicestudio-mac")
    legacy = make_app(tmp_path, "voicestudio-mac.git", service=True)

    assert repair.repair_startup(tmp_path, dry_run=False) == 0

    assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=false" in canonical.joinpath(
        "ENVIRONMENT"
    ).read_text()
    assert "PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED=false" in legacy.joinpath(
        "ENVIRONMENT"
    ).read_text()


def test_hub_service_installer_disables_competing_pinokio_autolaunch():
    installer = (ROOT / "install_service.sh").read_text()

    assert '"PINOKIO_SCRIPT_AUTOLAUNCH": "start.js"' in installer
    assert '"PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED": "false"' in installer
    assert installer.index('"PINOKIO_SCRIPT_AUTOLAUNCH_ENABLED": "false"') < installer.index(
        'touch "$ROOT/service/.installed"'
    )
