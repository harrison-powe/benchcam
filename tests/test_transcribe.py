"""Tests for Whisper-based marker auto-labeling.

The transcript->marker join (label_for_marker / plan_labels) is pure and is the
core of these tests. Whisper itself is always mocked — no real model is loaded
and no audio is decoded. Orchestration (run_transcribe) is tested with
find_capture/probe_has_audio/ffmpeg lookup and transcribe_audio monkeypatched.
"""

from __future__ import annotations

import pytest

from benchcam import transcribe as transcribe_mod
from benchcam.markers import read_markers
from benchcam.transcribe import (
    DEFAULT_MODEL,
    ENV_MODEL,
    TranscribeError,
    TranscriptSegment,
    label_for_marker,
    plan_labels,
    resolve_model,
    run_transcribe,
)


def seg(start, end, text):
    return TranscriptSegment(start, end, text)


# A small fake narration timeline used across the join tests. The segments are
# spaced well apart so a few-second window around one marker never bleeds into a
# neighbouring segment (except where a test deliberately uses a wide window).
TRANSCRIPT = [
    seg(0.0, 4.0, "getting started"),
    seg(9.0, 11.0, "power on"),
    seg(24.0, 26.0, "chip lifted"),
    seg(40.0, 42.0, "fault detected"),
]


# --------------------------------------------------------------------------- #
# label_for_marker (window join)
# --------------------------------------------------------------------------- #

def test_label_picks_segment_inside_window():
    # marker at 10s, window 5 -> [5, 15] catches only "power on" (9-11).
    assert label_for_marker(10.0, TRANSCRIPT, window=5.0) == "power on"


def test_label_joins_multiple_segments_in_order():
    # marker at 12.5, window 3 -> [9.5, 15.5] overlaps both 9-11 and 14-16.
    local = [seg(9.0, 11.0, "power on"), seg(14.0, 16.0, "chip lifted")]
    assert label_for_marker(12.5, local, window=3.0) == "power on chip lifted"


def test_label_empty_when_nothing_in_window():
    # marker at 33s, window 2 -> [31, 35], gap between 24-26 and 40-42.
    assert label_for_marker(33.0, TRANSCRIPT, window=2.0) == ""


def test_label_window_edge_is_inclusive_on_segment_end():
    # marker at 26s, window 0 -> [26,26]; segment 24-26 ends exactly at 26.
    assert label_for_marker(26.0, TRANSCRIPT, window=0.0) == "chip lifted"


def test_label_window_edge_is_inclusive_on_segment_start():
    # marker at 9s, window 0 -> [9,9]; segment 9-11 starts exactly at 9.
    assert label_for_marker(9.0, TRANSCRIPT, window=0.0) == "power on"


def test_label_just_outside_window_excluded():
    # marker at 8.9, window 0 -> [8.9,8.9]; segment starts at 9.0 -> excluded.
    assert label_for_marker(8.9, TRANSCRIPT, window=0.0) == ""


def test_label_emitted_in_chronological_order_regardless_of_input_order():
    unordered = [seg(14.0, 16.0, "second"), seg(9.0, 11.0, "first")]
    assert label_for_marker(12.5, unordered, window=3.0) == "first second"


# --------------------------------------------------------------------------- #
# plan_labels (which markers get labeled)
# --------------------------------------------------------------------------- #

def _markers(*rows):
    out = []
    for i, (elapsed, label, source) in enumerate(rows, start=1):
        out.append(
            {
                "marker_index": str(i),
                "elapsed_seconds": f"{elapsed:.3f}",
                "wall_time": "2026-06-22T12:00:00",
                "source": source,
                "label": label,
            }
        )
    return out


def test_plan_fills_only_empty_labels():
    markers = _markers(
        (10.0, "", "manual"),  # empty -> gets "power on"
        (15.0, "typed it myself", "manual"),  # has label -> skipped
    )
    plan = plan_labels(markers, TRANSCRIPT, window=5.0)
    assert [(a.marker_index, a.label) for a in plan] == [(1, "power on")]


def test_plan_overwrite_replaces_existing_labels():
    markers = _markers((25.0, "typed it myself", "manual"))
    plan = plan_labels(markers, TRANSCRIPT, window=5.0, overwrite=True)
    assert [(a.marker_index, a.label) for a in plan] == [(1, "chip lifted")]


def test_plan_skips_markers_with_no_transcript_text():
    # marker at 33s has nothing in window -> no assignment, not an empty label.
    markers = _markers((33.0, "", "manual"))
    assert plan_labels(markers, TRANSCRIPT, window=2.0) == []


def test_plan_tags_source_preserving_origin():
    markers = _markers((10.0, "", "external"))
    plan = plan_labels(markers, TRANSCRIPT, window=5.0)
    assert plan[0].source == "external+transcribed"


def test_plan_source_tag_is_idempotent():
    markers = _markers((10.0, "old", "manual+transcribed"))
    plan = plan_labels(markers, TRANSCRIPT, window=5.0, overwrite=True)
    assert plan[0].source == "manual+transcribed"


def test_plan_ignores_unparseable_rows():
    markers = [
        {"marker_index": "x", "elapsed_seconds": "10", "label": "", "source": "m"},
        {"marker_index": "1", "elapsed_seconds": "bad", "label": "", "source": "m"},
    ]
    assert plan_labels(markers, TRANSCRIPT, window=5.0) == []


# --------------------------------------------------------------------------- #
# Model resolution
# --------------------------------------------------------------------------- #

def test_resolve_model_flag_wins(monkeypatch):
    monkeypatch.setenv(ENV_MODEL, "small")
    assert resolve_model("medium") == "medium"


def test_resolve_model_env_fallback(monkeypatch):
    monkeypatch.setenv(ENV_MODEL, "small")
    assert resolve_model(None) == "small"


def test_resolve_model_default(monkeypatch):
    monkeypatch.delenv(ENV_MODEL, raising=False)
    assert resolve_model(None) == DEFAULT_MODEL


# --------------------------------------------------------------------------- #
# Missing Whisper -> clear error (no real import needed)
# --------------------------------------------------------------------------- #

def test_transcribe_audio_missing_whisper_raises(monkeypatch):
    def boom():
        raise TranscribeError(transcribe_mod._INSTALL_HINT)

    monkeypatch.setattr(transcribe_mod, "_import_whisper", boom)
    with pytest.raises(TranscribeError) as exc:
        transcribe_mod.transcribe_audio("capture.mp4", "base")
    assert "transcribe" in str(exc.value).lower()


# --------------------------------------------------------------------------- #
# Orchestration (Whisper + ffmpeg mocked)
# --------------------------------------------------------------------------- #

def _session_with_markers(tmp_path):
    from benchcam import session as session_mod

    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mp4").write_bytes(b"video")
    session_mod.start_session(session)
    return session


def _patch_environment(monkeypatch, segments, *, has_audio=True):
    monkeypatch.setattr(transcribe_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(transcribe_mod, "probe_has_audio", lambda *a, **k: has_audio)
    monkeypatch.setattr(
        transcribe_mod, "transcribe_audio", lambda *a, **k: segments
    )


def test_run_transcribe_labels_markers_and_writes_csv(tmp_path, monkeypatch):
    from benchcam import session as session_mod

    session = _session_with_markers(tmp_path)
    session_mod.add_marker(session, "", source="manual")  # at ~0s, empty
    # Force a known elapsed so it lands in the segment window.
    rows = read_markers(session.markers_file)
    rows[0]["elapsed_seconds"] = "10.000"
    _rewrite(session.markers_file, rows)

    _patch_environment(monkeypatch, [seg(9.0, 11.0, "power on")])

    messages: list[str] = []
    plan = run_transcribe(session.folder, window=5.0, out=messages.append)

    assert [(a.marker_index, a.label) for a in plan] == [(1, "power on")]
    written = read_markers(session.markers_file)
    assert written[0]["label"] == "power on"
    assert written[0]["source"] == "manual+transcribed"


def test_run_transcribe_keeps_manual_label_without_overwrite(tmp_path, monkeypatch):
    from benchcam import session as session_mod

    session = _session_with_markers(tmp_path)
    session_mod.add_marker(session, "operator note", source="manual")
    rows = read_markers(session.markers_file)
    rows[0]["elapsed_seconds"] = "10.000"
    _rewrite(session.markers_file, rows)

    _patch_environment(monkeypatch, [seg(9.0, 11.0, "power on")])

    plan = run_transcribe(session.folder, window=5.0, out=lambda _m: None)
    assert plan == []
    assert read_markers(session.markers_file)[0]["label"] == "operator note"


def test_run_transcribe_no_audio_skips(tmp_path, monkeypatch):
    session = _session_with_markers(tmp_path)
    _patch_environment(monkeypatch, [], has_audio=False)

    messages: list[str] = []
    plan = run_transcribe(session.folder, out=messages.append)
    assert plan == []
    assert any("no audio" in m.lower() for m in messages)


def test_run_transcribe_missing_ffmpeg_raises(tmp_path, monkeypatch):
    session = _session_with_markers(tmp_path)
    monkeypatch.setattr(transcribe_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(transcribe_mod, "probe_has_audio", lambda *a, **k: True)
    with pytest.raises(TranscribeError) as exc:
        run_transcribe(session.folder, out=lambda _m: None)
    assert "ffmpeg" in str(exc.value).lower()


def test_run_transcribe_negative_window_raises(tmp_path, monkeypatch):
    session = _session_with_markers(tmp_path)
    _patch_environment(monkeypatch, [])
    with pytest.raises(TranscribeError):
        run_transcribe(session.folder, window=-1.0, out=lambda _m: None)


def test_cli_transcribe_dispatches(tmp_path, monkeypatch):
    from benchcam.cli import main

    session = _session_with_markers(tmp_path)
    _patch_environment(monkeypatch, [], has_audio=False)

    code = main(
        [
            "transcribe",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--session",
            str(session.folder),
        ]
    )
    assert code == 0


def _rewrite(path, rows):
    import csv

    from benchcam.markers import FIELDNAMES

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, "") for f in FIELDNAMES})
