"""Tests for the interactive ``benchcam live`` shell.

Keypress and prompt input are injected so the loop can be exercised without a
real TTY. Markers must still be appended to markers.csv on every mark, while the
next index is tracked in memory.
"""

from __future__ import annotations

import pytest

from benchcam import session as session_mod
from benchcam.live import live_session
from benchcam.markers import read_markers
from benchcam.recorders.null import NullRecorder
from benchcam.session import SessionError


class SpyRecorder(NullRecorder):
    """NullRecorder that counts start/stop calls for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.start_calls = 0
        self.stop_calls = 0

    def start(self, storage_path) -> None:
        self.start_calls += 1
        super().start(storage_path)

    def stop(self) -> None:
        self.stop_calls += 1
        super().stop()


def _keys(sequence):
    """Return a read_key() that yields each key once, then EOF ("")."""
    it = iter(list(sequence))

    def _read() -> str:
        try:
            return next(it)
        except StopIteration:
            return ""  # EOF -> live treats as quit

    return _read


def _inputs(values):
    it = iter(list(values))

    def _input(prompt: str = "") -> str:
        return next(it)

    return _input


def _run(session, keys, *, recorder=None, inputs=(), out=None):
    recorder = recorder or SpyRecorder()
    out_lines: list[str] = []
    sink = out if out is not None else out_lines.append
    rc = live_session(
        session,
        recorder=recorder,
        read_key=_keys(keys),
        input_fn=_inputs(inputs),
        out=sink,
    )
    return rc, recorder, out_lines


def test_live_attaches_to_created_session_and_starts_it(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    assert session.status == session_mod.STATUS_CREATED

    rc, recorder, _ = _run(session, ["q"])

    assert rc == 0
    assert recorder.start_calls == 1  # same effect as `run`
    assert session.started_wall_time is not None


def test_live_does_not_restart_an_already_running_session(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    session_mod.start_session(session)

    _, recorder, _ = _run(session, ["q"])

    assert recorder.start_calls == 0  # attach without re-starting


def test_live_refuses_ended_session(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    session_mod.end_session(session)

    with pytest.raises(SessionError):
        _run(session, ["q"])


def test_space_and_enter_mark_increment_index_and_append(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)

    _run(session, [" ", "\r", "\n", "q"])

    rows = read_markers(session.markers_file)
    assert [int(r["marker_index"]) for r in rows] == [1, 2, 3]
    assert all(r["source"] == "manual" for r in rows)
    assert all(r["label"] == "" for r in rows)


def test_label_key_prompts_and_records_label(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)

    _run(session, ["l", "l", "q"], inputs=["chip lifted", ""])

    rows = read_markers(session.markers_file)
    assert [r["label"] for r in rows] == ["chip lifted", ""]
    assert [int(r["marker_index"]) for r in rows] == [1, 2]
    assert all(r["source"] == "manual" for r in rows)


def test_index_continues_when_mark_used_before_live(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    session_mod.start_session(session)
    # Simulate a `benchcam mark` before entering the live shell.
    session_mod.add_marker(session, "pre-live")

    _run(session, [" ", "q"])

    rows = read_markers(session.markers_file)
    assert [r["label"] for r in rows] == ["pre-live", ""]
    assert [int(r["marker_index"]) for r in rows] == [1, 2]


def test_in_memory_index_does_not_reread_file(tmp_path):
    """Even if markers.csv is truncated mid-session, the index keeps climbing."""
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)

    keys = iter([" ", " ", "q"])

    def read_key() -> str:
        try:
            return next(keys)
        except StopIteration:
            return ""

    # First mark, then wipe the file's data rows before the second mark.
    captured: list[str] = []

    def out(line: str) -> None:
        captured.append(str(line))
        if line.strip().startswith("marker #1"):
            # Truncate to header only; an index recomputed from disk would
            # restart at 1, but the in-memory counter must give #2.
            from benchcam.markers import init_markers_file

            init_markers_file(session.markers_file)

    live_session(
        session,
        recorder=SpyRecorder(),
        read_key=read_key,
        out=out,
    )

    assert any(line.strip().startswith("marker #2") for line in captured)


def test_note_key_appends_line_to_notes(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)

    _run(session, ["n", "q"], inputs=["voltage looked off"])

    notes = session.notes_file.read_text(encoding="utf-8")
    assert "voltage looked off" in notes


def test_status_key_reports_session_and_count(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)

    _, _, out_lines = _run(session, [" ", "s", "q"])

    status_lines = [ln for ln in out_lines if "markers 1" in ln]
    assert status_lines, out_lines
    assert session.session_id in status_lines[0]


def test_quit_ends_session_and_stops_recorder(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)

    rc, recorder, _ = _run(session, ["q"])

    assert rc == 0
    assert recorder.stop_calls == 1
    reloaded = session_mod.load_session(session.folder)
    assert reloaded.status == session_mod.STATUS_ENDED
    assert reloaded.ended_wall_time is not None


def test_eof_is_treated_as_quit(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)

    rc, recorder, _ = _run(session, [])  # no keys -> immediate EOF

    assert rc == 0
    assert recorder.stop_calls == 1
    assert session_mod.load_session(session.folder).status == session_mod.STATUS_ENDED
