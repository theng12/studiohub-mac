"""Fixed Studio Hub dependency convergence commands."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Sequence


APP = Path(__file__).resolve().parents[1]
MODES = {"base", "all-installed"}


def uv_executable() -> str:
    """Return only Pinokio's configured uv executable, or fail closed."""
    config = Path.home() / ".pinokio" / "config.json"
    try:
        home = json.loads(config.read_text(encoding="utf-8"))["home"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Configured Pinokio home is unavailable.") from exc
    if not isinstance(home, str):
        raise RuntimeError("Configured Pinokio home is unavailable.")
    root = Path(home).expanduser()
    if not root.is_absolute():
        raise RuntimeError("Configured Pinokio home must be absolute.")
    uv = root.resolve() / "bin" / "miniforge" / "bin" / "uv"
    if not uv.is_file():
        raise RuntimeError("Configured Pinokio uv executable is unavailable.")
    return str(uv)


def converge(mode: str, *, runner=subprocess.run) -> None:
    """Converge fixed Hub requirements without accepting caller-provided commands."""
    if mode == "generation":
        raise ValueError("Hub has no generation stack")
    if mode not in MODES:
        raise ValueError("mode must be base or all-installed")
    runner(
        [uv_executable(), "pip", "install", "--python", sys.executable,
         "-r", str(APP / "requirements.lock")],
        cwd=APP, check=True, timeout=1200,
    )


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 1 or values[0] not in MODES:
        print("dependency convergence: invalid mode", file=sys.stderr)
        return 2
    converge(values[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
