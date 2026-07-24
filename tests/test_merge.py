"""Tests for benchcam merge (multi-session join).

The ffmpeg/ffprobe layer is mocked (no real video): _concat writes a stub merged
file, _probe_streams / probe_duration / _stream_end_pts return controlled values.
The logic under test is the offset/renumber of BOTH markers and mute spans, the
hard refusals (non-ended, incompatible streams, presence mismatch, short join,
too-few sources, free space), source-order warning, notes merge, provenance, and
that sources are never modified and derived artifacts are dropped. Real-data
acceptance (the hand-merge cross-check, real A/V sync, BOM-free) runs separately.
"""

from __future__ import annotations

import csv
import hashlib
import types
from pathlib import Path

import pytest

from benchcam import merge as merge_mod
from benchcam import session as session_mod
from benchcam.editor import read_mute_spans
from benchcam.markers import Marker, append_marker, read_markers
from benchcam.merge import MergeError, _MUTE_SPAN_FIELDNAMES, run_merge

_VIDEO = {"codec_name": "mjpeg", "width": 1920, "height": 1080,
          "r_frame_rate": "30/1", "pix_fmt": "yuvj422p"}
_AUDIO = {"codec_name": "aac", "sample_rate": "44100", "channels": 2}


def _streams(video=True, audio=True):
    return {"video": dict(_VIDEO) if video else None,
            "audio": dict(_AUDIO) if audio else None}


def _source(root, *, markers=(), spans=(), notes=None, end=True):
    session = session_mod.create_session(root=root, recorder="ffmpeg")
    session_mod.start_session(session)
    (session.folder / "capture.mkv").write_bytes(b"video-bytes-" + session.session_id.encode())
    for i, (elapsed, label, src) in enumerate(markers, 1):
        append_marker(session.markers_file,
                      Marker(i, elapsed, f"2026-07-22T10:00:{i:02d}-07:00", src, label))
    if spans:
        with (session.folder / "mute_spans.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=_MUTE_SPAN_FIELDNAMES)
            w.writeheader()
            for i, (start, end_s) in enumerate(spans, 1):
                w.writerow({"start_seconds": f"{start:.3f}", "end_seconds": f"{end_s:.3f}",
                            "start_wall_time": "w", "end_wall_time": "w",
                            "span_index": i, "source": "gpio"})
    if notes is not None:
        (session.folder / "notes.md").write_text(notes, encoding="utf-8")
    if end:
        session_mod.end_session(session)
    return session


def _mock(monkeypatch, *, durations, merged=None, streams=None, av=None, free=10 ** 13):
    monkeypatch.setattr(merge_mod.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(merge_mod.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(total=free, used=0, free=free))

    def fake_dur(path, *, ffprobe="ffprobe"):
        name = Path(path).parent.name
        if name in durations:
            return durations[name]
        return merged if merged is not None else sum(durations.values())

    monkeypatch.setattr(merge_mod, "probe_duration", fake_dur)
    st = streams or {}
    monkeypatch.setattr(merge_mod, "_probe_streams",
                        lambda cap, ffprobe: st.get(Path(cap).parent.name, _streams()))

    def fake_concat(sources, dest_capture, ffmpeg, out):
        Path(dest_capture).write_bytes(b"MERGED")

    monkeypatch.setattr(merge_mod, "_concat", fake_concat)
    monkeypatch.setattr(merge_mod, "_stream_end_pts",
                        lambda path, stream, dur, ffprobe: (av or {}).get(stream))


# --------------------------------------------------------------------------- #
# THE HEADLINE: reproduce the hand-built episode #02 merge arithmetic
# --------------------------------------------------------------------------- #

def test_merge_reproduces_hand_merge_marker_offsets(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root, markers=[(55.932, "", "gpio")])
    b = _source(root, markers=[(24.979, "", "gpio"), (118.526, "", "gpio")])
    _mock(monkeypatch, durations={a.session_id: 141.598, b.session_id: 160.575},
          av={"v:0": 302.196, "a:0": 302.196})

    dest = run_merge([a.session_id, b.session_id], sessions_root=root, out=lambda _m: None)

    rows = read_markers(dest / "markers.csv")
    assert [r["marker_index"] for r in rows] == ["1", "2", "3"]
    # A's marker unmoved; B's two offset by A's exact 141.598s duration.
    assert [r["elapsed_seconds"] for r in rows] == ["55.932", "166.577", "260.124"]


# --------------------------------------------------------------------------- #
# THE GAP: mute spans get the identical offset, land at the right times
# --------------------------------------------------------------------------- #

def test_merge_offsets_mute_spans_identically_and_they_land_right(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root, markers=[(10.0, "", "gpio")], spans=[(20.0, 30.0)])
    b = _source(root, spans=[(5.0, 15.0)])
    _mock(monkeypatch, durations={a.session_id: 141.598, b.session_id: 100.0})

    dest = run_merge([a.session_id, b.session_id], sessions_root=root, out=lambda _m: None)

    spans = read_markers(dest / "mute_spans.csv")
    assert [(s["span_index"], s["start_seconds"], s["end_seconds"]) for s in spans] == [
        ("1", "20.000", "30.000"),    # A: offset 0
        ("2", "146.598", "156.598"),  # B: offset 141.598 (same as markers)
    ]
    # The editor's own reader lands them on the merged timeline (not merely copied).
    assert read_mute_spans(dest) == [(20.0, 30.0), (146.598, 156.598)]


def test_merge_writes_no_mute_spans_file_when_no_source_has_any(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root, markers=[(10.0, "", "gpio")])
    b = _source(root)
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 100.0})

    dest = run_merge([a.session_id, b.session_id], sessions_root=root, out=lambda _m: None)
    assert not (dest / "mute_spans.csv").exists()


# --------------------------------------------------------------------------- #
# N-ary cumulative offset (REQUIRED: a d_A-every-time bug is invisible with two)
# --------------------------------------------------------------------------- #

def test_merge_three_sources_use_cumulative_prefix_sum_offsets(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root, markers=[(1.0, "", "gpio")])
    b = _source(root, markers=[(2.0, "", "gpio")])
    c = _source(root, markers=[(3.0, "", "gpio")])
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 10.0, c.session_id: 1.0})

    dest = run_merge([a.session_id, b.session_id, c.session_id],
                     sessions_root=root, out=lambda _m: None)

    rows = read_markers(dest / "markers.csv")
    # offsets: A=0, B=100, C=110 (prefix sums). A d_A-every-time bug would give C=101.
    assert [r["elapsed_seconds"] for r in rows] == ["1.000", "102.000", "113.000"]


# --------------------------------------------------------------------------- #
# Hard refusals (each writes nothing)
# --------------------------------------------------------------------------- #

def _no_merged_folder(root):
    return list(Path(root).glob("*_merged")) == []


def test_merge_refuses_non_ended_source(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root, markers=[(10.0, "", "gpio")])
    b = _source(root, markers=[(5.0, "", "gpio")], end=False)  # still RUNNING
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 100.0})

    with pytest.raises(MergeError) as exc:
        run_merge([a.session_id, b.session_id], sessions_root=root, out=lambda _m: None)
    assert b.session_id in str(exc.value) and "ended" in str(exc.value)
    assert _no_merged_folder(root)


def test_merge_refuses_incompatible_video_params(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root)
    b = _source(root)
    odd = _streams()
    odd["video"]["width"] = 1280  # different geometry
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 100.0},
          streams={b.session_id: odd})

    with pytest.raises(MergeError) as exc:
        run_merge([a.session_id, b.session_id], sessions_root=root, out=lambda _m: None)
    assert "parameter mismatch" in str(exc.value)
    assert _no_merged_folder(root)


def test_merge_refuses_stream_presence_mismatch(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root)
    b = _source(root)
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 100.0},
          streams={b.session_id: _streams(audio=False)})  # A has audio, B doesn't

    with pytest.raises(MergeError) as exc:
        run_merge([a.session_id, b.session_id], sessions_root=root, out=lambda _m: None)
    assert "presence mismatch" in str(exc.value)
    assert _no_merged_folder(root)


def test_merge_short_duration_deletes_partial_and_refuses(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root, markers=[(10.0, "", "gpio")])
    b = _source(root, markers=[(5.0, "", "gpio")])
    # merged comes back far short of 200 -> truncation.
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 100.0}, merged=100.0)

    with pytest.raises(MergeError) as exc:
        run_merge([a.session_id, b.session_id], sessions_root=root, out=lambda _m: None)
    assert "short" in str(exc.value) and "truncat" in str(exc.value).lower()
    assert _no_merged_folder(root)  # partial output removed


def test_merge_requires_at_least_two_sources(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root)
    _mock(monkeypatch, durations={a.session_id: 100.0})
    with pytest.raises(MergeError) as exc:
        run_merge([a.session_id], sessions_root=root, out=lambda _m: None)
    assert "at least two" in str(exc.value)


def test_merge_refuses_when_free_space_insufficient(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root)
    b = _source(root)
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 100.0}, free=1)
    with pytest.raises(MergeError) as exc:
        run_merge([a.session_id, b.session_id], sessions_root=root, out=lambda _m: None)
    assert "free space" in str(exc.value)
    assert _no_merged_folder(root)


# --------------------------------------------------------------------------- #
# Source order: argument order used, warns when not chronological
# --------------------------------------------------------------------------- #

def test_merge_warns_when_argument_order_not_chronological(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root, markers=[(10.0, "", "gpio")])  # created first
    b = _source(root, markers=[(20.0, "", "gpio")])  # created second
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 100.0})

    msgs = []
    # Pass B, A: reverse of chronological.
    dest = run_merge([b.session_id, a.session_id], sessions_root=root, out=msgs.append)
    text = "\n".join(msgs)
    assert "does NOT match chronological" in text
    assert a.session_id in text and b.session_id in text
    # ARGUMENT order honored: B's marker first (offset 0), then A (offset 100).
    rows = read_markers(dest / "markers.csv")
    assert [r["elapsed_seconds"] for r in rows] == ["20.000", "110.000"]


# --------------------------------------------------------------------------- #
# session.json rebuild + provenance
# --------------------------------------------------------------------------- #

def test_merge_session_json_has_provenance_and_rebuilds_fields(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root, markers=[(10.0, "", "gpio")])
    b = _source(root, markers=[(5.0, "", "gpio")])
    _mock(monkeypatch, durations={a.session_id: 141.598, b.session_id: 160.575})

    dest = run_merge([a.session_id, b.session_id], sessions_root=root,
                     name="Episode 02", out=lambda _m: None)

    merged = session_mod.load_session(dest)  # round-trips through from_dict
    assert merged.status == "ended"
    assert merged.merged_from == [a.session_id, b.session_id]
    assert merged.merge_source_durations == [141.598, 160.575]
    assert merged.name == "Episode 02"
    a_sess = session_mod.load_session(a.folder)
    b_sess = session_mod.load_session(b.folder)
    assert merged.created_wall_time == a_sess.created_wall_time   # from first
    assert merged.started_wall_time == a_sess.started_wall_time   # baseline = first
    assert merged.ended_wall_time == b_sess.ended_wall_time       # from last


# --------------------------------------------------------------------------- #
# notes.md merge (skip boilerplate; carry one if all boilerplate)
# --------------------------------------------------------------------------- #

def test_merge_notes_keeps_real_notes_and_skips_boilerplate(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root, notes="# Notes for session A\n\nreal content from A\n")
    b = _source(root)  # default boilerplate template
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 100.0})

    dest = run_merge([a.session_id, b.session_id], sessions_root=root, out=lambda _m: None)

    notes = (dest / "notes.md").read_text(encoding="utf-8")
    assert "real content from A" in notes
    assert notes.count("## From ") == 1  # only A contributed; B's boilerplate skipped


def test_merge_notes_all_boilerplate_carries_one_clean_header(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root)
    b = _source(root)
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 100.0})

    dest = run_merge([a.session_id, b.session_id], sessions_root=root, out=lambda _m: None)
    notes = (dest / "notes.md").read_text(encoding="utf-8")
    assert "## From " not in notes  # no duplicated templates
    assert notes.startswith(f"# Notes for session {dest.name}")


# --------------------------------------------------------------------------- #
# Sources untouched; derived artifacts dropped
# --------------------------------------------------------------------------- #

def _digest(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(folder).iterdir()) if p.is_file()}


def test_merge_never_modifies_sources(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root, markers=[(10.0, "", "gpio")], spans=[(1.0, 2.0)],
                notes="# Notes for session A\n\nkeep me\n")
    b = _source(root, markers=[(5.0, "", "gpio")])
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 100.0})

    before_a, before_b = _digest(a.folder), _digest(b.folder)
    run_merge([a.session_id, b.session_id], sessions_root=root, out=lambda _m: None)
    assert _digest(a.folder) == before_a
    assert _digest(b.folder) == before_b


def test_merge_drops_derived_artifacts(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root, markers=[(10.0, "", "gpio")])
    b = _source(root, markers=[(5.0, "", "gpio")])
    # A source that was already edited carries derived artifacts.
    for name in ("transcript.json", "review.mp4", "chapters.json",
                 "chapters.txt", "youtube_chapters.txt", "markers.csv.bak"):
        (a.folder / name).write_text("stale", encoding="utf-8")
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 100.0})

    dest = run_merge([a.session_id, b.session_id], sessions_root=root, out=lambda _m: None)

    for name in ("transcript.json", "review.mp4", "chapters.json",
                 "chapters.txt", "youtube_chapters.txt", "markers.csv.bak"):
        assert not (dest / name).exists(), name
    # only the source-of-truth files exist
    assert {p.name for p in dest.iterdir()} == {"capture.mkv", "markers.csv", "notes.md",
                                                "session.json"}


# --------------------------------------------------------------------------- #
# A/V sync reporting (gap 3)
# --------------------------------------------------------------------------- #

def test_merge_reports_av_sync_and_warns_on_divergence(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    a = _source(root)
    b = _source(root)
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 100.0},
          av={"v:0": 200.0, "a:0": 200.9})  # 0.9s drift > tolerance

    msgs = []
    run_merge([a.session_id, b.session_id], sessions_root=root, out=msgs.append)
    text = "\n".join(msgs)
    assert "A/V sync" in text
    assert "diverge" in text and "0.900" in text


# --------------------------------------------------------------------------- #
# CLI dispatch
# --------------------------------------------------------------------------- #

def test_cli_merge_dispatches(tmp_path, monkeypatch):
    from benchcam.cli import main

    root = tmp_path / "sessions"
    a = _source(root, markers=[(10.0, "", "gpio")])
    b = _source(root, markers=[(5.0, "", "gpio")])
    _mock(monkeypatch, durations={a.session_id: 100.0, b.session_id: 100.0})

    code = main(["merge", "--sessions-root", str(root), a.session_id, b.session_id])
    assert code == 0
    assert list(root.glob("*_merged"))
