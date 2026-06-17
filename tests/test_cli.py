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

    # end clears the active pointer; load the session folder directly.
    folders = [p for p in (tmp_path / "sessions").iterdir() if p.is_dir()]
    assert len(folders) == 1
    session = session_mod.load_session(folders[0])
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


def test_run_with_obs_stub_reports_error(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root, "--recorder", "obs"]) == 0
    rc = main(["run", "--sessions-root", root])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not implemented" in err.lower()


def test_mark_without_note_still_works(tmp_path):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root]) == 0
    assert main(["run", "--sessions-root", root]) == 0
    assert main(["mark", "--sessions-root", root, "first motion"]) == 0

    session = session_mod.get_active_session(tmp_path / "sessions")
    rows = read_markers(session.markers_file)
    assert rows[0]["label"] == "first motion"
    assert rows[0]["note"] == ""


def test_mark_with_note_appends_note(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root]) == 0
    assert main(["run", "--sessions-root", root]) == 0
    rc = main(
        [
            "mark",
            "--sessions-root",
            root,
            "first motion",
            "--note",
            "manual observation",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "manual observation" in out

    session = session_mod.get_active_session(tmp_path / "sessions")
    rows = read_markers(session.markers_file)
    assert rows[0]["label"] == "first motion"
    assert rows[0]["note"] == "manual observation"


def test_status_active_session(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root, "--profile", "bench-a"]) == 0
    assert main(["run", "--sessions-root", root]) == 0
    assert main(["mark", "--sessions-root", root, "power on"]) == 0

    rc = main(["status", "--sessions-root", root])
    assert rc == 0
    out = capsys.readouterr().out
    assert "status:" in out
    assert "running" in out
    assert "recorder:" in out
    assert "markers:    1" in out


def test_status_explicit_session_path(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root]) == 0
    session = session_mod.get_active_session(tmp_path / "sessions")

    rc = main(["status", "--session", session.storage_path])
    assert rc == 0
    out = capsys.readouterr().out
    assert session.session_id in out


def test_status_no_active_session_handled(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    rc = main(["status", "--sessions-root", root])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No active session" in err


def test_end_clears_active_session(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root]) == 0
    assert main(["run", "--sessions-root", root]) == 0
    assert main(["end", "--sessions-root", root]) == 0

    assert session_mod.active_session_name(tmp_path / "sessions") is None

    rc = main(["status", "--sessions-root", root])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No active session" in err
