"""Local, gitignored config for the dashboard (e.g. OBS WebSocket settings).

So the dashboard can be launched from a desktop shortcut without setting a global
``BENCHCAM_OBS_PASSWORD`` env var, the OBS connection settings entered in the
browser are persisted to ``.benchcam/config.json`` (under the launch directory,
i.e. the project root). That folder is git-ignored so the password is never
committed. The file is plain JSON, local-only.

Stdlib only (``json``, ``pathlib``).
"""

from __future__ import annotations

import json
from pathlib import Path

CONFIG_DIRNAME = ".benchcam"
CONFIG_FILENAME = "config.json"


def default_config_root() -> Path:
    """A stable config root that does not depend on the current directory.

    The dashboard is launched in different ways (shortcut, `.vbs`, `.bat`, a
    terminal), so anchoring config to ``Path.cwd()`` made the saved OBS password
    appear "forgotten" when the launch directory differed. For the documented
    editable install (`pip install -e .`), this module lives at
    ``<repo>/src/benchcam/config.py``, so the repo root is ``parents[2]`` — we use
    it when it looks like the project (``pyproject.toml``/``.git`` present), which
    keeps the gitignored ``<repo>/.benchcam/config.json`` stable across launches.
    Otherwise (e.g. a non-editable install) we fall back to the home directory.
    """
    repo = Path(__file__).resolve().parents[2]
    if (repo / "pyproject.toml").exists() or (repo / ".git").exists():
        return repo
    return Path.home()


def config_path(root: Path | str | None = None) -> Path:
    """Path to the config file (``<root>/.benchcam/config.json``).

    ``root`` defaults to :func:`default_config_root` (a stable, cwd-independent
    location), so the saved config is found regardless of how the dashboard was
    launched.
    """
    base = Path(root) if root is not None else default_config_root()
    return base / CONFIG_DIRNAME / CONFIG_FILENAME


def load_config(root: Path | str | None = None) -> dict:
    """Load the config dict, or ``{}`` if it is missing or unreadable."""
    path = config_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data: dict, root: Path | str | None = None) -> Path:
    """Write the config dict atomically; returns the file path."""
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
