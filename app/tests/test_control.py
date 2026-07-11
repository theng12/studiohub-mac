from backend import control


def test_bundled_pterm_uses_bundled_node():
    pterm = str(control.PINOKIO_HOME / "bin" / "npm" / "bin" / "pterm")
    command = control.pterm_command(pterm, "start", "update.js", "pinokio://test")
    assert command[-4:] == ["start", "update.js", "--ref", "pinokio://test"]
    node = control.PINOKIO_HOME / "bin" / "miniforge" / "bin" / "node"
    if node.exists():
        assert command[:2] == [str(node), pterm]
