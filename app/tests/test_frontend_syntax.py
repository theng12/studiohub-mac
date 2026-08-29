"""The dashboard is one hand-edited file; a syntax slip blanks the whole page.

The other frontend tests run *extracted fragments* through node, so a fragment
can parse perfectly while the file around it is broken. This parses what the
browser actually receives.
"""

import re
import subprocess
from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[2] / "app/frontend/index.html"


def test_every_dashboard_script_block_parses():
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", FRONTEND.read_text(), re.S)

    assert blocks, "the dashboard has no inline script to check"
    for index, block in enumerate(blocks):
        result = subprocess.run(
            ["node", "--check", "-"], input=block,
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, (
            f"script block {index} does not parse:\n{result.stderr[:2000]}"
        )
