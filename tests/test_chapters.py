"""Tests for benchcam chapters (review sheet + editable round-trip) and the
Part-A chapters.json review-time map written by edit."""

from __future__ import annotations

import json

from benchcam import chapters as chapters_mod
from benchcam import editor as editor_mod
from benchcam import session as session_mod
from benchcam.markers import Marker, append_marker, read_markers


def _session(tmp_path):
    root = tmp_path / "sessions"
    return session_mod.create_session(root=root)


def _rows(session):
    return {int(r["marker_index"]): r for r in read_markers(session.markers_file)}


# --------------------------------------------------------------------------- #
# Part A: chapters.json review times match the segment plan
# --------------------------------------------------------------------------- #

def _out_bases(plan):
    bases, acc = [], 0.0
    for seg in plan:
        bases.append(acc)
        acc += seg.output_duration
    return bases


def _expected_review(src, plan, bases):
    for i, seg in enumerate(plan):
        if seg.start <= src < seg.end:
            return round(bases[i] + (src - seg.start) / seg.speed, 3)
    return None


def test_chapter_review_records_match_segment_plan(tmp_path):
    rows = [
        {"marker_index": "1", "elapsed_seconds": "10.000", "wall_time": "",
         "source": "manual", "label": "Alpha", "narration": ""},
        {"marker_index": "2", "elapsed_seconds": "50.000", "wall_time": "",
         "source": "gpio", "label": "Beta", "narration": ""},
    ]
    events = [(10.0, "Alpha"), (50.0, "Beta")]
    plan = editor_mod.build_segment_plan(events, 100.0, pre=3.0, post=5.0, speed=10.0)
    recs = editor_mod.chapter_review_records(rows, plan, 100.0, pre=3.0)
    bases = _out_bases(plan)

    assert [r["label"] for r in recs] == ["Alpha", "Beta"]
    assert [r["marker_index"] for r in recs] == [1, 2]
    assert [r["elapsed_seconds"] for r in recs] == [10.0, 50.0]
    # review time == the caption's local-time position (src_start = elapsed - pre).
    assert recs[0]["review_seconds"] == _expected_review(7.0, plan, bases)
    assert recs[1]["review_seconds"] == _expected_review(47.0, plan, bases)


def test_chapter_review_records_two_markers_in_one_normal_segment(tmp_path):
    # Markers 4s apart: windows [7,15] and [11,19] merge into ONE normal segment
    # [7,19]; the second chapter must get its offset within that segment, not the
    # segment's start.
    rows = [
        {"marker_index": "1", "elapsed_seconds": "10.000", "wall_time": "",
         "source": "manual", "label": "Alpha", "narration": ""},
        {"marker_index": "2", "elapsed_seconds": "14.000", "wall_time": "",
         "source": "manual", "label": "Beta", "narration": ""},
    ]
    events = [(10.0, "Alpha"), (14.0, "Beta")]
    plan = editor_mod.build_segment_plan(events, 100.0, pre=3.0, post=5.0, speed=10.0)
    recs = editor_mod.chapter_review_records(rows, plan, 100.0, pre=3.0)

    # Alpha at the normal-segment start (src 7); Beta 4s later inside it (src 11).
    assert recs[0]["review_seconds"] == 0.7  # out of lapse [0,7] @10x
    assert recs[1]["review_seconds"] == 4.7  # 0.7 + (11 - 7)/1.0
    assert recs[0]["review_seconds"] != recs[1]["review_seconds"]


def test_run_edit_writes_chapters_json(tmp_path, monkeypatch):
    from unittest import mock

    session = _session(tmp_path)
    (session.folder / "capture.mp4").write_bytes(b"video")
    session_mod.start_session(session)
    append_marker(session.markers_file, Marker(1, 10.0, "", "manual", "Alpha"))

    monkeypatch.setattr(editor_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(editor_mod, "probe_duration", lambda *a, **k: 100.0)
    monkeypatch.setattr(editor_mod, "probe_has_audio", lambda *a, **k: False)
    monkeypatch.setattr(
        editor_mod, "run_ffmpeg_command", lambda cmd: mock.Mock(returncode=0, stderr="")
    )

    editor_mod.run_edit(session.folder, out=lambda _m: None)

    data = json.loads((session.folder / "chapters.json").read_text(encoding="utf-8"))
    assert data["review_filename"] == "review.mp4"
    assert [c["label"] for c in data["chapters"]] == ["Alpha"]
    assert data["chapters"][0]["marker_index"] == 1


# --------------------------------------------------------------------------- #
# Part B: the sheet
# --------------------------------------------------------------------------- #

def _seed_chapters(session):
    append_marker(session.markers_file, Marker(1, 10.0, "w", "gpio", "Alpha"))
    append_marker(session.markers_file, Marker(2, 50.0, "w", "manual", "Beta"))
    append_marker(session.markers_file, Marker(3, 90.0, "w", "auto", "Gamma", narration="the gamma quote"))
    append_marker(session.markers_file, Marker(4, 95.0, "w", "manual", ""))  # unlabeled


def test_sheet_lists_chapters_with_review_and_quote(tmp_path):
    session = _session(tmp_path)
    _seed_chapters(session)
    (session.folder / "chapters.json").write_text(json.dumps({
        "review_filename": "review.mp4",
        "chapters": [
            {"marker_index": 1, "review_seconds": 0.7, "label": "Alpha"},
            {"marker_index": 2, "review_seconds": 11.9, "label": "Beta"},
            {"marker_index": 3, "review_seconds": 20.0, "label": "Gamma"},
        ],
    }), encoding="utf-8")

    lines = []
    chapters_mod.run_chapters(session.folder, out=lines.append)
    text = "\n".join(lines)

    assert "Alpha" in text and "Beta" in text and "Gamma" in text
    assert "the gamma quote" in text  # narration used as the source quote
    assert "00:00.7" in text  # review time rendered mm:ss.s
    assert "3 chapter(s)" in text  # unlabeled marker #4 is not a chapter


def test_sheet_fallback_without_render(tmp_path):
    session = _session(tmp_path)
    _seed_chapters(session)  # no chapters.json written

    lines = []
    chapters_mod.run_chapters(session.folder, out=lines.append)
    text = "\n".join(lines)

    assert "no render yet" in text
    assert "--:--" in text  # review column blank
    assert "Alpha" in text and "Gamma" in text  # still lists titles


# --------------------------------------------------------------------------- #
# Part C: editable round-trip + staleness guard
# --------------------------------------------------------------------------- #

def _retitle(session, mapping):
    """Edit chapters.txt in place: {old_title: new_title}."""
    path = session.folder / "chapters.txt"
    text = path.read_text(encoding="utf-8")
    for old, new in mapping.items():
        text = text.replace(f"] {old}    #", f"] {new}    #")
    path.write_text(text, encoding="utf-8")


def test_edit_apply_round_trip_changes_only_labels(tmp_path):
    session = _session(tmp_path)
    _seed_chapters(session)
    before = _rows(session)

    chapters_mod.run_chapters(session.folder, edit=True, out=lambda _m: None)
    _retitle(session, {"Alpha": "Alpha Prime", "Gamma": "Gamma Two"})
    chapters_mod.run_chapters(session.folder, apply=True, out=lambda _m: None)

    after = _rows(session)
    # Only the two edited labels changed.
    assert after[1]["label"] == "Alpha Prime"
    assert after[3]["label"] == "Gamma Two"
    assert after[2]["label"] == "Beta"  # untouched (not edited)
    # Everything else on every row is byte-identical: source, elapsed, narration.
    for idx in (1, 2, 3, 4):
        for field in ("marker_index", "elapsed_seconds", "wall_time", "source", "narration"):
            assert after[idx][field] == before[idx][field], (idx, field)
    # The unlabeled marker #4 is still unlabeled and untouched.
    assert after[4]["label"] == ""


def test_apply_skips_malformed_lines(tmp_path):
    session = _session(tmp_path)
    append_marker(session.markers_file, Marker(1, 10.0, "w", "manual", "Alpha"))

    (session.folder / "chapters.txt").write_text(
        "# header comment\n"
        "[1@10.000] Renamed    # review 00:00.7  raw 00:10.0  | \"q\"\n"
        "this is not a valid line at all\n"
        "[99@10.000] Ghost    # no marker with index 99\n",
        encoding="utf-8",
    )

    warnings = []
    chapters_mod.run_chapters(session.folder, apply=True, out=warnings.append)

    assert _rows(session)[1]["label"] == "Renamed"  # the good line applied
    text = "\n".join(warnings)
    assert "not a '[index@elapsed] Title' line" in text  # the garbage line
    assert "[99]" in text and "no marker" in text  # the unknown index
    assert "Applied 1 title(s); skipped 2." in text


def test_apply_refuses_stale_line_and_applies_valid_ones(tmp_path):
    from benchcam.markers import update_marker

    session = _session(tmp_path)
    append_marker(session.markers_file, Marker(1, 10.0, "w", "gpio", "Alpha"))
    append_marker(session.markers_file, Marker(2, 50.0, "w", "manual", "Beta"))

    chapters_mod.run_chapters(session.folder, edit=True, out=lambda _m: None)
    _retitle(session, {"Alpha": "Alpha New", "Beta": "Beta New"})
    # Simulate an intervening regeneration that MOVED marker #2 (e.g. autochapter
    # --overwrite reindexing): its elapsed no longer matches the chapters.txt stamp.
    update_marker(session.markers_file, 2, {"elapsed_seconds": "60.000"})

    warnings = []
    chapters_mod.run_chapters(session.folder, apply=True, out=warnings.append)

    after = _rows(session)
    assert after[1]["label"] == "Alpha New"  # stamp still valid -> applied
    assert after[2]["label"] == "Beta"  # stale -> refused, original title kept
    assert after[2]["elapsed_seconds"] == "60.000"  # untouched
    text = "\n".join(warnings)
    assert "[2]" in text and "stale" in text
    assert "Applied 1 title(s); skipped 1." in text
