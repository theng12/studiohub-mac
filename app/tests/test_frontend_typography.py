import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "app/frontend/index.html"


def _frontend_source() -> str:
    return FRONTEND.read_text()


def test_dashboard_defines_the_shared_typography_scale():
    source = _frontend_source()

    expected_tokens = {
        "--font-compact": "12px",
        "--font-secondary": "13px",
        "--font-body": "14px",
        "--font-control": "15px",
        "--font-picker-option": "16px",
        "--control-min-height": "40px",
    }
    for name, value in expected_tokens.items():
        assert re.search(rf"{re.escape(name)}\s*:\s*{re.escape(value)}\b", source)


def test_dashboard_has_no_readable_font_below_twelve_pixels():
    source = _frontend_source()
    declarations = []

    for match in re.finditer(r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)px", source):
        declarations.append((float(match.group(1)), match.group(0)))
    for match in re.finditer(
        r"font\s*:[^;{}]*?\s([0-9]+(?:\.[0-9]+)?)px(?:/[^\s;{}]+)?",
        source,
    ):
        declarations.append((float(match.group(1)), match.group(0)))

    too_small = [declaration for size, declaration in declarations if size < 12]
    assert too_small == []

    relative_sizes = [
        (float(match.group(1)), match.group(0))
        for match in re.finditer(
            r"font-size\s*:\s*([0-9]*\.?[0-9]+)(?:em|rem)", source
        )
    ]
    shrinking_sizes = [
        declaration for size, declaration in relative_sizes if size < 1
    ]
    assert shrinking_sizes == []


def test_native_pickers_use_the_readable_option_size():
    source = re.sub(r"\s+", "", _frontend_source())

    assert (
        "select,selectoption,selectoptgroup"
        "{font-size:var(--font-picker-option)}"
    ) in source
    assert (
        ".selctl,.toolbarinput,.toolbarselect,.jforminput,.jformselect,.jformtextarea"
        "{min-height:var(--control-min-height);"
    ) in source
    assert "code{font-family:var(--mono);font-size:inherit;" in source
