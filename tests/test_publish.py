"""Tests for benchcam publish (YouTube chapter block export)."""

from __future__ import annotations

import json

import pytest

from benchcam import publish as publish_mod
from benchcam import session as session_mod
from benchcam.markers import Marker, append_marker
from benchcam.publish import PublishError, build_chapter_block, run_publish


def _session(tmp_path):
    return session_mod.create_session(root=tmp_path / "sessions")


def _write_chapters_json(session, entries):
    """entries: list of (marker_index, review_seconds, label)."""
    (session.folder / "chapters.json").write_text(
        json.dumps({
            "review_filename": "review.mp4",
            "chapters": [
                {"marker_index": mi, "chapter_number": i + 1,
                 "review_seconds": rev, "label": label}
                for i, (mi, rev, label) in enumerate(entries)
            ],
        }),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# 1. Normal block, ascending, current titles from markers.csv
# --------------------------------------------------------------------------- #

def test_normal_block_uses_current_titles_in_review_order(tmp_path):
    session = _session(tmp_path)
    # markers.csv "New A" overrides the stale "Old A" in chapters.json (title join).
    append_marker(session.markers_file, Marker(1, 5.0, "", "manual", "New A"))
    append_marker(session.markers_file, Marker(2, 9.0, "", "manual", "Beta"))
    append_marker(session.markers_file, Marker(3, 20.0, "", "auto", "Gamma"))
    append_marker(session.markers_file, Marker(4, 40.0, "", "manual", "Delta"))
    _write_chapters_json(session, [
        (1, 0.0, "Old A"), (2, 20.0, "Beta"), (3, 40.0, "Gamma"), (4, 60.0, "Delta"),
    ])

    lines = []
    assert run_publish(session.folder, out=lines.append) == 0
    text = "\n".join(lines)

    block = [ln for ln in lines if ln and ln[0].isdigit()]
    assert block == ["0:00 New A", "0:20 Beta", "0:40 Gamma", "1:00 Delta"]
    assert "warning:" not in text and "note:" not in text
    # File holds ONLY the paste-ready block, nothing else.
    written = (session.folder / "youtube_chapters.txt").read_text(encoding="utf-8")
    assert written == "0:00 New A\n0:20 Beta\n0:40 Gamma\n1:00 Delta\n"


# --------------------------------------------------------------------------- #
# 2. Chapters <10s apart: keep the first, drop + warn the later
# --------------------------------------------------------------------------- #

def test_chapter_under_10s_apart_is_dropped_and_warned(tmp_path):
    session = _session(tmp_path)
    for mi, (rev, label) in enumerate(
        [(0.0, "A"), (5.0, "B"), (20.0, "C"), (40.0, "D")], start=1
    ):
        append_marker(session.markers_file, Marker(mi, rev, "", "manual", label))
    _write_chapters_json(session, [
        (1, 0.0, "A"), (2, 5.0, "B"), (3, 20.0, "C"), (4, 40.0, "D"),
    ])

    lines = []
    run_publish(session.folder, out=lines.append)
    text = "\n".join(lines)

    block = [ln for ln in lines if ln and ln[0].isdigit()]
    assert block == ["0:00 A", "0:20 C", "0:40 D"]  # B (0:05) dropped
    assert "warning:" in text and "Dropped 'B'" in text
    # 3 survive -> no <3 warning.
    assert "at least 3" not in text


# --------------------------------------------------------------------------- #
# 3. First chapter not at 0: re-anchor to 0:00 (before the dedup), note it
# --------------------------------------------------------------------------- #

def test_first_chapter_reanchored_to_zero(tmp_path):
    session = _session(tmp_path)
    for mi, (rev, label) in enumerate(
        [(15.0, "A"), (30.0, "B"), (50.0, "C")], start=1
    ):
        append_marker(session.markers_file, Marker(mi, rev, "", "manual", label))
    _write_chapters_json(session, [(1, 15.0, "A"), (2, 30.0, "B"), (3, 50.0, "C")])

    lines = []
    run_publish(session.folder, out=lines.append)
    text = "\n".join(lines)

    block = [ln for ln in lines if ln and ln[0].isdigit()]
    assert block == ["0:00 A", "0:30 B", "0:50 C"]  # A moved 0:15 -> 0:00
    assert "note:" in text and "moved to 0:00" in text
    assert "0:15" in text  # the note reports the original time


def test_reanchor_runs_before_dedup_so_no_false_drop(tmp_path):
    # First at 0:12, second at 0:15: raw gap 3s. Re-anchoring first to 0:00 makes
    # the gap 15s, so the second must survive (dedup runs AFTER re-anchor).
    data = {"chapters": [
        {"marker_index": 1, "review_seconds": 12.0, "label": "A"},
        {"marker_index": 2, "review_seconds": 15.0, "label": "B"},
        {"marker_index": 3, "review_seconds": 30.0, "label": "C"},
    ]}
    block = build_chapter_block(data, [])
    assert block.lines == ["0:00 A", "0:15 B", "0:30 C"]
    assert block.warnings == []  # nothing dropped


# --------------------------------------------------------------------------- #
# 4. Over an hour uses H:MM:SS
# --------------------------------------------------------------------------- #

def test_over_an_hour_uses_h_mm_ss(tmp_path):
    data = {"chapters": [
        {"marker_index": 1, "review_seconds": 0.0, "label": "A"},
        {"marker_index": 2, "review_seconds": 1800.0, "label": "B"},
        {"marker_index": 3, "review_seconds": 3665.4, "label": "C"},
    ]}
    block = build_chapter_block(data, [])
    assert block.lines == ["0:00 A", "30:00 B", "1:01:05 C"]  # floor(3665.4)=3665


# --------------------------------------------------------------------------- #
# 5. Missing chapters.json -> clear error pointing at 'edit'
# --------------------------------------------------------------------------- #

def test_missing_chapters_json_errors(tmp_path):
    session = _session(tmp_path)  # no render -> no chapters.json
    with pytest.raises(PublishError) as exc:
        run_publish(session.folder, out=lambda _m: None)
    message = str(exc.value).lower()
    assert "chapters.json" in message and "edit" in message


# --------------------------------------------------------------------------- #
# 6. Under 3 chapters still emits, with a warning; file is block-only
# --------------------------------------------------------------------------- #

def test_under_three_chapters_warns_but_still_emits(tmp_path):
    session = _session(tmp_path)
    append_marker(session.markers_file, Marker(1, 0.0, "", "manual", "Alpha"))
    append_marker(session.markers_file, Marker(2, 30.0, "", "manual", "Beta"))
    _write_chapters_json(session, [(1, 0.0, "Alpha"), (2, 30.0, "Beta")])

    lines = []
    run_publish(session.folder, out=lines.append)
    text = "\n".join(lines)

    assert "warning:" in text and "at least 3" in text
    # The two lines are still emitted, and the FILE contains only them (no warning).
    written = (session.folder / "youtube_chapters.txt").read_text(encoding="utf-8")
    assert written == "0:00 Alpha\n0:30 Beta\n"
    assert "warning" not in written and "note" not in written
