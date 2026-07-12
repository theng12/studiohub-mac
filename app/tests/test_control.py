from backend import control


def test_bundled_pterm_uses_bundled_node():
    pterm = str(control.PINOKIO_HOME / "bin" / "npm" / "bin" / "pterm")
    command = control.pterm_command(pterm, "start", "update.js", "pinokio://test")
    assert command[-4:] == ["start", "update.js", "--ref", "pinokio://test"]
    node = control.PINOKIO_HOME / "bin" / "miniforge" / "bin" / "node"
    if node.exists():
        assert command[:2] == [str(node), pterm]


def test_app_folder_resolution_accepts_exact_git_suffix_variant(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "PINOKIO_HOME", tmp_path)
    actual = tmp_path / "api" / "imagestudio-mac.git"
    actual.mkdir(parents=True)
    assert control.resolve_app_dir({"app": "imagestudio-mac"}) == actual
    assert control.resolve_app_dir({"app": "imagestudio-mac.git"}) == actual
