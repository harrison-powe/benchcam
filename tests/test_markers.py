"""Tests for marker writing."""

from __future__ import annotations

import csv

from benchcam import session as session_mod
from benchcam.markers import FIELDNAMES, read_markers


def test_markers_file_has_header(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    with session.markers_file.open(encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert header == FIELDNAMES


def test_add_marker_appends_rows(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    session_mod.start_session(session)

    m1 = session_mod.add_marker(session, "first")
    m2 = session_mod.add_marker(session, "second", source="external")

    assert m1.marker_index == 1
    assert m2.marker_index == 2

    rows = read_markers(session.markers_file)
    assert len(rows) == 2
    assert rows[0]["label"] == "first"
    assert rows[0]["source"] == "manual"
    assert rows[1]["label"] == "second"
    assert rows[1]["source"] == "external"
    assert int(rows[1]["marker_index"]) == 2


def test_markers_header_includes_note_column(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    with session.markers_file.open(encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert header[-1] == "note"
    assert header == FIELDNAMES


def test_add_marker_without_note_defaults_empty(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    marker = session_mod.add_marker(session, "no note here")
    assert marker.note == ""

    rows = read_markers(session.markers_file)
    assert rows[0]["note"] == ""


def test_add_marker_with_note_is_persisted(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    marker = session_mod.add_marker(
        session, "first motion", note="actuator moved after wiring fix"
    )
    assert marker.note == "actuator moved after wiring fix"

    rows = read_markers(session.markers_file)
    assert rows[0]["label"] == "first motion"
    assert rows[0]["note"] == "actuator moved after wiring fix"


def test_marker_elapsed_is_non_negative_and_numeric(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    session_mod.start_session(session)

    marker = session_mod.add_marker(session, "x")
    assert marker.elapsed_seconds >= 0.0

    rows = read_markers(session.markers_file)
    elapsed = float(rows[0]["elapsed_seconds"])
    assert elapsed >= 0.0


def test_marker_index_persists_across_loads(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    session_mod.add_marker(session, "a")

    reloaded = session_mod.load_session(session.folder)
    m = session_mod.add_marker(reloaded, "b")
    assert m.marker_index == 2

    rows = read_markers(reloaded.markers_file)
    assert [r["label"] for r in rows] == ["a", "b"]


def test_marker_with_comma_in_label_is_escaped(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    session_mod.add_marker(session, "lifted chip, then waited")

    rows = read_markers(session.markers_file)
    assert rows[0]["label"] == "lifted chip, then waited"
