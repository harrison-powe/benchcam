"""Guards for the Windows dashboard launchers (no real Windows needed).

These just assert the launcher files exist and invoke the dashboard the way the
README documents, so a typo or accidental deletion is caught.
"""

from __future__ import annotations

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_noconsole_vbs_launcher_exists_and_runs_dashboard_hidden():
    vbs = (SCRIPTS / "benchcam-dashboard.vbs").read_text(encoding="utf-8")
    # Uses the windowless venv Python and runs the dashboard module.
    assert "pythonw.exe" in vbs
    assert "-m benchcam dashboard" in vbs
    # Launched hidden (window style 0) and non-blocking so the server stays up.
    assert ".Run " in vbs and ", 0, False" in vbs


def test_bat_launcher_still_present():
    bat = (SCRIPTS / "benchcam-dashboard.bat").read_text(encoding="utf-8")
    assert "benchcam dashboard" in bat
    assert (SCRIPTS / "benchcam-dashboard.ps1").exists()
