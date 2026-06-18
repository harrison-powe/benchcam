"""End-to-end tests for the CLI flow (new -> run -> mark -> end)."""

from __future__ import annotations

from benchcam import session as session_mod
from benchcam.cli import main
from benchcam.markers import read_markers


def test_full_cli_flow(tmp_path, capsys):
    root = str(tmp_path / "sessions")

    assert main(["new", "--sessions-root", root, "--profile", "demo"]) == 0
    assert main(["run", "--sessions-root", root]) == 0
    assert main(["mark", "--sessions-root", root, "power on"]) == 0
    assert main(["mark", "--sessions-root", root, "fault", "--source", "external"]) == 0
    assert main(["end", "--sessions-root", root]) == 0

    session = session_mod.get_active_session(tmp_path / "sessions")
    assert session.status == session_mod.STATUS_ENDED

    rows = read_markers(session.markers_file)
    assert [r["label"] for r in rows] == ["power on", "fault"]
    assert rows[1]["source"] == "external"


def test_mark_without_session_returns_error(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    rc = main(["mark", "--sessions-root", root, "oops"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No active session" in err


def test_sessions_root_env_var_is_default_when_flag_omitted(tmp_path, monkeypatch):
    # Setting BENCHCAM_SESSIONS_ROOT (e.g. to an external SSD path) makes new
    # sessions land there without passing --sessions-root.
    root = tmp_path / "ssd" / "benchcam-sessions"
    monkeypatch.setenv("BENCHCAM_SESSIONS_ROOT", str(root))

    assert main(["new", "--profile", "ssd-test"]) == 0

    active = session_mod.get_active_session(root)
    assert active.profile == "ssd-test"
    assert str(root) in active.storage_path


def test_run_with_obs_reports_clear_error_without_extra(tmp_path, capsys):
    # Without the optional obsws-python extra installed (or without OBS
    # running), selecting the obs recorder must fail cleanly with an actionable
    # message rather than silently falling back to null.
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root, "--recorder", "obs"]) == 0
    rc = main(["run", "--sessions-root", root])
    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "obs" in err
