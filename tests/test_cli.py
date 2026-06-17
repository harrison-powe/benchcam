"""End-to-end tests for the CLI flow (new -> run -> mark -> end)."""

from __future__ import annotations

from benchcam import artifacts as artifacts_mod
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


def test_run_interactive_full_flow(tmp_path, capsys, monkeypatch):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root, "--profile", "bench-a"]) == 0

    lines = iter(
        [
            "m first motion | actuator moved after wiring fix",
            "note swapped encoder cable",
            "status",
            "end",
        ]
    )

    def fake_input(_prompt=""):
        try:
            return next(lines)
        except StopIteration as exc:
            raise EOFError from exc

    monkeypatch.setattr("builtins.input", fake_input)

    rc = main(["run", "--interactive", "--sessions-root", root])
    assert rc == 0

    out = capsys.readouterr().out
    assert "actuator moved after wiring fix" in out
    assert "status:" in out

    # Session folder still inspectable after end (active pointer cleared).
    folders = [p for p in (tmp_path / "sessions").iterdir() if p.is_dir()]
    assert len(folders) == 1
    session = session_mod.load_session(folders[0])
    assert session.status == session_mod.STATUS_ENDED

    rows = read_markers(session.markers_file)
    assert rows[0]["label"] == "first motion"
    assert rows[0]["note"] == "actuator moved after wiring fix"
    assert rows[0]["source"] == "keyboard"

    notes = session.notes_file.read_text(encoding="utf-8")
    assert "swapped encoder cable" in notes


def test_run_interactive_no_active_session(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    rc = main(["run", "--interactive", "--sessions-root", root])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No active session" in err


def test_attach_media_copy_active_session(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root]) == 0

    src = tmp_path / "obs-recording.mp4"
    src.write_bytes(b"video bytes")

    rc = main(["attach-media", str(src), "--sessions-root", root, "--label", "main OBS recording"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Attached media" in out

    session = session_mod.get_active_session(tmp_path / "sessions")
    assert (session.media_dir / "obs-recording.mp4").exists()
    rows = artifacts_mod.read_artifacts(session.artifacts_file)
    assert rows[0]["label"] == "main OBS recording"
    assert rows[0]["stored_path"] == "media/obs-recording.mp4"
    assert rows[0]["mode"] == "copy"


def test_attach_media_reference_mode(tmp_path):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root]) == 0

    src = tmp_path / "big.mkv"
    src.write_bytes(b"x" * 100)

    rc = main(["attach-media", str(src), "--sessions-root", root, "--mode", "reference"])
    assert rc == 0

    session = session_mod.get_active_session(tmp_path / "sessions")
    assert list(session.media_dir.iterdir()) == []
    rows = artifacts_mod.read_artifacts(session.artifacts_file)
    assert rows[0]["mode"] == "reference"
    assert rows[0]["stored_path"] == ""


def test_attach_media_explicit_session(tmp_path):
    root = str(tmp_path / "sessions")
    # Create two sessions; the second becomes active.
    assert main(["new", "--sessions-root", root]) == 0
    first = session_mod.get_active_session(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root]) == 0

    src = tmp_path / "clip.mov"
    src.write_bytes(b"data")

    rc = main(["attach-media", str(src), "--session", first.storage_path])
    assert rc == 0

    rows = artifacts_mod.read_artifacts(first.artifacts_file)
    assert len(rows) == 1
    assert rows[0]["kind"] == "video"


def test_attach_media_no_active_session(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")
    rc = main(["attach-media", str(src), "--sessions-root", root])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No active session" in err


def test_attach_media_missing_file(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root]) == 0
    rc = main(["attach-media", str(tmp_path / "nope.mp4"), "--sessions-root", root])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_attach_media_directory_source(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root]) == 0
    d = tmp_path / "adir"
    d.mkdir()
    rc = main(["attach-media", str(d), "--sessions-root", root])
    assert rc == 1
    err = capsys.readouterr().err
    assert "directory" in err.lower()


def test_review_active_session(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root]) == 0
    assert main(["run", "--sessions-root", root]) == 0
    assert main(["mark", "--sessions-root", root, "first motion"]) == 0

    rc = main(["review", "--sessions-root", root])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Wrote review" in out

    session = session_mod.get_active_session(tmp_path / "sessions")
    review_path = session.folder / "review.md"
    assert review_path.exists()
    text = review_path.read_text(encoding="utf-8")
    assert "# BenchCam Session Review" in text
    assert "first motion" in text


def test_review_explicit_session(tmp_path):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root]) == 0
    first = session_mod.get_active_session(tmp_path / "sessions")
    # Make a second active session; review should still target the explicit one.
    assert main(["new", "--sessions-root", root]) == 0

    rc = main(["review", "--session", first.storage_path])
    assert rc == 0
    assert (first.folder / "review.md").exists()


def test_review_custom_output(tmp_path):
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root]) == 0
    session = session_mod.get_active_session(tmp_path / "sessions")
    target = tmp_path / "out" / "my-review.md"

    rc = main(["review", "--sessions-root", root, "--output", str(target)])
    assert rc == 0
    assert target.exists()
    assert not (session.folder / "review.md").exists()


def test_review_no_active_session(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    rc = main(["review", "--sessions-root", root])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No active session" in err


def test_review_invalid_session_path(tmp_path, capsys):
    rc = main(["review", "--session", str(tmp_path / "nope")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No session.json" in err


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
