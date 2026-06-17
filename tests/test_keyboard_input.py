"""Tests for the line-based interactive marker loop."""

from __future__ import annotations

from benchcam import session as session_mod
from benchcam.inputs.keyboard_input import MARKER_SOURCE, run_interactive_loop
from benchcam.markers import read_markers
from benchcam.recorders import NullRecorder


def _fake_reader(lines):
    """Return a read_line callable that yields each line then raises EOFError."""
    it = iter(lines)

    def read_line(_prompt=""):
        try:
            return next(it)
        except StopIteration as exc:
            raise EOFError from exc

    return read_line


def _make_running_session(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    session_mod.start_session(session)
    return session


def _run(session, lines):
    out = []
    recorder = NullRecorder()
    run_interactive_loop(
        session,
        recorder,
        read_line=_fake_reader(lines),
        emit=out.append,
    )
    return out, recorder


def test_interactive_marker_without_note(tmp_path):
    session = _make_running_session(tmp_path)
    _run(session, ["m first motion", "end"])

    rows = read_markers(session.markers_file)
    assert len(rows) == 1
    assert rows[0]["label"] == "first motion"
    assert rows[0]["note"] == ""
    assert rows[0]["source"] == MARKER_SOURCE


def test_interactive_marker_with_note(tmp_path):
    session = _make_running_session(tmp_path)
    _run(session, ["m first motion | actuator moved after wiring fix", "end"])

    rows = read_markers(session.markers_file)
    assert len(rows) == 1
    assert rows[0]["label"] == "first motion"
    assert rows[0]["note"] == "actuator moved after wiring fix"
    assert rows[0]["source"] == MARKER_SOURCE


def test_interactive_note_appends_to_notes_md(tmp_path):
    session = _make_running_session(tmp_path)
    _run(session, ["note swapped encoder cable before retry", "end"])

    contents = session.notes_file.read_text(encoding="utf-8")
    assert "swapped encoder cable before retry" in contents
    # Timestamped, single appended line in the documented format.
    note_lines = [
        ln for ln in contents.splitlines() if "swapped encoder cable" in ln
    ]
    assert len(note_lines) == 1
    assert note_lines[0].startswith("- [")


def test_interactive_status_prints_summary(tmp_path):
    session = _make_running_session(tmp_path)
    out, _ = _run(session, ["status", "end"])

    joined = "\n".join(out)
    assert "status:" in joined
    assert "recorder:" in joined
    assert "markers:" in joined


def test_interactive_end_ends_session_and_exits(tmp_path):
    session = _make_running_session(tmp_path)
    out, recorder = _run(session, ["end"])

    reloaded = session_mod.load_session(session.folder)
    assert reloaded.status == session_mod.STATUS_ENDED
    assert recorder.is_running is False
    assert any("Ended session" in line for line in out)


def test_interactive_blank_and_unknown_lines(tmp_path):
    session = _make_running_session(tmp_path)
    out, _ = _run(session, ["", "   ", "bogus", "end"])

    joined = "\n".join(out)
    assert "Unknown command" in joined
    # Blank lines should not have produced markers.
    assert read_markers(session.markers_file) == []


def test_interactive_eof_ends_session(tmp_path):
    session = _make_running_session(tmp_path)
    # No 'end' line: reader runs out and raises EOFError -> clean end.
    _run(session, ["m only marker"])

    reloaded = session_mod.load_session(session.folder)
    assert reloaded.status == session_mod.STATUS_ENDED


def test_interactive_marker_missing_label_shows_usage(tmp_path):
    session = _make_running_session(tmp_path)
    out, _ = _run(session, ["m", "end"])

    assert any("usage: m" in line for line in out)
    assert read_markers(session.markers_file) == []
