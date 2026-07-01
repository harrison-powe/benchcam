"""Tests for the marker-aware auto-editor.

Pure functions (segment plan + filtergraph construction) are tested directly;
the orchestration is tested with ffmpeg/ffprobe mocked. No real encode runs.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from benchcam import editor as editor_mod
from benchcam.editor import (
    EditError,
    Segment,
    build_ffmpeg_edit_command,
    build_filter_complex,
    build_segment_plan,
    find_capture,
    resolve_session_dir,
)
from benchcam.editor import _escape_drawtext, _escape_fontfile


def _kinds(plan):
    return [("normal" if s.normal else "lapse") for s in plan]


def _spans(plan):
    return [(round(s.start, 3), round(s.end, 3)) for s in plan]


# --------------------------------------------------------------------------- #
# Segment plan
# --------------------------------------------------------------------------- #

def test_plan_basic_windows_and_lapse_fill():
    # Markers spaced out so windows don't merge (the user's ~18.8/34.9/47.1 case).
    events = [(18.8, "power on"), (34.9, "chip lifted"), (47.1, "fault")]
    plan = build_segment_plan(events, duration=60.0, pre=3.0, post=5.0, speed=8.0)

    assert _kinds(plan) == [
        "lapse", "normal", "lapse", "normal", "lapse", "normal", "lapse",
    ]
    assert _spans(plan) == [
        (0.0, 15.8),
        (15.8, 23.8),
        (23.8, 31.9),
        (31.9, 39.9),
        (39.9, 44.1),
        (44.1, 52.1),
        (52.1, 60.0),
    ]
    # Normal segments are 1x and keep their captions; lapse segments are 8x.
    normals = [s for s in plan if s.normal]
    assert all(s.speed == 1.0 for s in normals)
    assert all(s.speed == 8.0 for s in plan if not s.normal)
    assert [c.text for s in normals for c in s.captions] == [
        "power on", "chip lifted", "fault",
    ]


def test_plan_merges_overlapping_and_adjacent_windows():
    # 10 -> [7,15], 13 -> [10,18] overlap -> merge to [7,18].
    plan = build_segment_plan(
        [(10.0, "a"), (13.0, "b")], duration=30.0, pre=3.0, post=5.0, speed=8.0
    )
    assert _kinds(plan) == ["lapse", "normal", "lapse"]
    assert _spans(plan) == [(0.0, 7.0), (7.0, 18.0), (18.0, 30.0)]
    # Both labels live in the single merged normal segment.
    normal = plan[1]
    assert [c.text for c in normal.captions] == ["a", "b"]


def test_plan_adjacent_windows_touch_and_merge():
    # window ends at 15, next starts at 15 -> adjacent -> merged (no 0s lapse).
    plan = build_segment_plan(
        [(10.0, "a"), (18.0, "b")], duration=40.0, pre=3.0, post=5.0, speed=8.0
    )
    # m1 [7,15], m2 [15,23] -> merge [7,23]
    assert _spans(plan) == [(0.0, 7.0), (7.0, 23.0), (23.0, 40.0)]
    assert _kinds(plan) == ["lapse", "normal", "lapse"]


def test_plan_clamps_marker_beyond_duration():
    plan = build_segment_plan(
        [(100.0, "late")], duration=30.0, pre=3.0, post=5.0, speed=8.0
    )
    # clamped to 30 -> window [27, 30], lapse fills [0,27]; no trailing lapse.
    assert _spans(plan) == [(0.0, 27.0), (27.0, 30.0)]
    assert _kinds(plan) == ["lapse", "normal"]


def test_plan_no_markers_is_full_timelapse():
    plan = build_segment_plan([], duration=42.0, speed=8.0)
    assert len(plan) == 1
    assert plan[0].normal is False
    assert _spans(plan) == [(0.0, 42.0)]
    assert plan[0].speed == 8.0


def test_plan_marker_at_start_has_no_leading_lapse():
    plan = build_segment_plan([(1.0, "x")], duration=20.0, pre=3.0, post=5.0)
    # window [0, 6] (pre clamped to 0)
    assert _spans(plan) == [(0.0, 6.0), (6.0, 20.0)]
    assert _kinds(plan) == ["normal", "lapse"]


def test_plan_unlabeled_marker_gets_no_caption():
    plan = build_segment_plan([(10.0, "")], duration=30.0)
    normal = [s for s in plan if s.normal][0]
    assert normal.captions == []


# --------------------------------------------------------------------------- #
# drawtext / fontfile escaping (the Windows fix)
# --------------------------------------------------------------------------- #

def test_escape_fontfile_windows_path():
    # C:\Windows\Fonts\arial.ttf -> forward slashes, drive colon escaped \\:
    assert _escape_fontfile(r"C:\Windows\Fonts\arial.ttf") == r"C\\:/Windows/Fonts/arial.ttf"


def test_escape_drawtext_colon():
    assert _escape_drawtext("it's 3:00") == r"it\'s 3\\:00"


def test_escape_drawtext_apostrophe():
    assert _escape_drawtext("don't") == r"don\'t"


def test_escape_drawtext_comma():
    assert _escape_drawtext("lifted, waited") == r"lifted\, waited"


def test_escape_drawtext_backslash():
    # a single literal backslash -> four (survives both parser levels)
    assert _escape_drawtext("a\\b") == "a\\\\\\\\b"


def test_escape_drawtext_percent():
    assert _escape_drawtext("50% done") == r"50\% done"


# --------------------------------------------------------------------------- #
# filtergraph construction
# --------------------------------------------------------------------------- #

def test_filtergraph_builds_segments_speeds_and_concat():
    plan = [
        Segment(0.0, 10.0, normal=False, speed=8.0),
        Segment(10.0, 18.0, normal=True, speed=1.0,
                captions=[editor_mod.Caption("power on", 0.0, 8.0)]),
    ]
    fc = build_filter_complex(plan, fontfile=None, has_audio=True)

    fmt = editor_mod._AUDIO_SEGMENT_FORMAT
    # Lapse segment: trimmed and sped up 8x, audio from the silent input.
    assert "[0:v]trim=start=0.000:end=10.000,setpts=(PTS-STARTPTS)/8[v0]" in fc
    assert f"[1:a]atrim=start=0:end=1.250,asetpts=PTS-STARTPTS,{fmt}[a0]" in fc  # 10/8
    # Normal segment: 1x, real audio, caption burned in (literal, expansion off).
    assert "[0:v]trim=start=10.000:end=18.000,setpts=PTS-STARTPTS" in fc
    assert "drawtext=text=power on:expansion=none" in fc
    assert "enable=between(t\\,0.000\\,8.000)" in fc
    assert f"[0:a]atrim=start=10.000:end=18.000,asetpts=PTS-STARTPTS,{fmt}[a1]" in fc
    assert "concat=n=2:v=1:a=1[outv][outa]" in fc


def test_filtergraph_includes_escaped_fontfile_when_given():
    plan = [
        Segment(0.0, 8.0, normal=True, speed=1.0,
                captions=[editor_mod.Caption("power on", 0.0, 8.0)]),
    ]
    fc = build_filter_complex(plan, fontfile=r"C:\Windows\Fonts\arial.ttf")
    assert r"fontfile=C\\:/Windows/Fonts/arial.ttf" in fc


def test_filtergraph_silent_audio_for_all_segments_when_source_has_no_audio():
    plan = [
        Segment(0.0, 10.0, normal=False, speed=8.0),
        Segment(10.0, 18.0, normal=True, speed=1.0),
    ]
    fc = build_filter_complex(plan, has_audio=False)
    fmt = editor_mod._AUDIO_SEGMENT_FORMAT
    assert "[0:a]" not in fc  # never references the source audio
    assert f"[1:a]atrim=start=0:end=8.000,asetpts=PTS-STARTPTS,{fmt}[a1]" in fc  # normal


def test_filtergraph_no_drawtext_without_labels():
    plan = build_segment_plan([(10.0, "")], duration=30.0)
    fc = build_filter_complex(plan)
    assert "drawtext" not in fc


def test_filtergraph_escapes_tricky_label():
    plan = [
        Segment(0.0, 8.0, normal=True, speed=1.0,
                captions=[editor_mod.Caption("it's 3:00", 0.0, 8.0)]),
    ]
    fc = build_filter_complex(plan)
    assert r"text=it\'s 3\\:00" in fc


def test_every_audio_segment_pins_an_explicit_sample_format():
    # Regression guard: without an explicit sample format on EVERY audio segment,
    # concat negotiates the lowest common format across segments. The silent
    # anullsrc filler emits 8-bit u8 on some ffmpeg builds, which would drag the
    # real narration down to 8-bit and add quantization static at the seams. Each
    # [aN] label must therefore be produced by an aformat with a real float
    # sample_fmt, for both real-audio and silent-only graphs.
    import re

    for has_audio in (True, False):
        plan = build_segment_plan(
            [(10.0, "power on"), (40.0, "fault")], duration=90.0
        )
        fc = build_filter_complex(plan, has_audio=has_audio)
        labels = re.findall(r"\[a(\d+)\]", fc)
        # Every audio segment label (excluding the concat consumer side) resolves
        # to a chain ending in the explicit sample-format pin.
        n_segments = len(plan)
        for k in range(n_segments):
            assert f"{editor_mod._AUDIO_SEGMENT_FORMAT}[a{k}]" in fc, (
                f"segment a{k} is not pinned to an explicit sample format"
            )
        # And the pin must actually request a float sample_fmt (not u8/s16).
        assert "sample_fmts=fltp" in editor_mod._AUDIO_SEGMENT_FORMAT
        assert labels  # sanity: the graph really has audio segments


def test_filtergraph_empty_plan_raises():
    with pytest.raises(EditError):
        build_filter_complex([])


def test_command_uses_filter_complex_script_and_output(tmp_path):
    script = tmp_path / "graph.ffscript"
    cmd = build_ffmpeg_edit_command(
        tmp_path / "capture.mp4", tmp_path / "review.mp4", script, ffmpeg="ffmpeg"
    )
    assert cmd[0] == "ffmpeg"
    assert str(tmp_path / "capture.mp4") in cmd
    assert "anullsrc=channel_layout=stereo:sample_rate=44100" in cmd
    assert cmd[cmd.index("-filter_complex_script") + 1] == str(script)
    assert "-filter_complex" not in cmd  # uses the script form, not inline
    assert cmd[cmd.index("-map") + 1] == "[outv]"
    assert "libx264" in cmd and "aac" in cmd
    assert cmd[-1] == str(tmp_path / "review.mp4")


# --------------------------------------------------------------------------- #
# Session / capture resolution
# --------------------------------------------------------------------------- #

def _make_session(root, name, *, with_capture=True, capture_name="capture.mp4"):
    d = root / name
    d.mkdir(parents=True)
    (d / "session.json").write_text("{}", encoding="utf-8")
    if with_capture:
        (d / capture_name).write_bytes(b"video")
    return d


def test_resolve_session_dir_newest(tmp_path):
    root = tmp_path / "sessions"
    _make_session(root, "2026-06-18_01-00-00")
    newest = _make_session(root, "2026-06-18_05-00-00")
    assert resolve_session_dir(root, None) == newest


def test_resolve_session_dir_by_id(tmp_path):
    root = tmp_path / "sessions"
    s = _make_session(root, "2026-06-18_01-00-00")
    assert resolve_session_dir(root, "2026-06-18_01-00-00") == s


def test_resolve_session_dir_unknown_raises(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    with pytest.raises(EditError):
        resolve_session_dir(root, "nope")


def test_find_capture_prefers_mp4(tmp_path):
    d = _make_session(tmp_path, "S", capture_name="capture.mkv")
    (d / "capture.mp4").write_bytes(b"v")
    assert find_capture(d).name == "capture.mp4"


def test_find_capture_falls_back_to_obs_pointer(tmp_path):
    d = _make_session(tmp_path, "S", with_capture=False)
    external = tmp_path / "obs" / "rec.mkv"
    external.parent.mkdir(parents=True)
    external.write_bytes(b"v")
    (d / "obs_recording.txt").write_text(str(external), encoding="utf-8")
    assert find_capture(d) == external


def test_find_capture_missing_raises(tmp_path):
    d = _make_session(tmp_path, "S", with_capture=False)
    with pytest.raises(EditError):
        find_capture(d)


# --------------------------------------------------------------------------- #
# Orchestration (mocked ffmpeg/ffprobe)
# --------------------------------------------------------------------------- #

def test_run_edit_builds_command_and_returns_output(tmp_path, monkeypatch):
    from benchcam import session as session_mod

    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mp4").write_bytes(b"video")
    session_mod.start_session(session)
    session_mod.add_marker(session, "power on")

    monkeypatch.setattr(editor_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(editor_mod, "probe_duration", lambda *a, **k: 60.0)
    monkeypatch.setattr(editor_mod, "probe_has_audio", lambda *a, **k: True)

    captured = {}

    def fake_run(cmd):
        captured["cmd"] = cmd
        # The filtergraph lives in the temp script; read it before run_edit
        # cleans it up so we can assert on its contents.
        script = cmd[cmd.index("-filter_complex_script") + 1]
        captured["graph"] = Path(script).read_text(encoding="utf-8")
        return mock.Mock(returncode=0, stderr="")

    monkeypatch.setattr(editor_mod, "run_ffmpeg_command", fake_run)

    output = editor_mod.run_edit(session.folder, out=lambda _m: None)

    assert output == session.folder / "review.mp4"
    assert captured["cmd"][-1] == str(session.folder / "review.mp4")
    assert "-filter_complex_script" in captured["cmd"]
    assert "drawtext=text=power on:expansion=none" in captured["graph"]
    # The temp filterscript is cleaned up afterwards (only review.mp4 remains).
    script_path = captured["cmd"][captured["cmd"].index("-filter_complex_script") + 1]
    assert not Path(script_path).exists()


def test_run_edit_missing_ffmpeg_raises(tmp_path, monkeypatch):
    d = _make_session(tmp_path, "S")
    monkeypatch.setattr(editor_mod.shutil, "which", lambda name: None)
    with pytest.raises(EditError) as exc:
        editor_mod.run_edit(d, out=lambda _m: None)
    assert "ffmpeg" in str(exc.value).lower()


def test_run_edit_speed_must_exceed_one(tmp_path, monkeypatch):
    d = _make_session(tmp_path, "S")
    monkeypatch.setattr(editor_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(EditError):
        editor_mod.run_edit(d, speed=1.0, out=lambda _m: None)


def test_run_edit_no_markers_announces_full_timelapse(tmp_path, monkeypatch):
    from benchcam import session as session_mod

    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mp4").write_bytes(b"video")

    monkeypatch.setattr(editor_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(editor_mod, "probe_duration", lambda *a, **k: 30.0)
    monkeypatch.setattr(editor_mod, "probe_has_audio", lambda *a, **k: False)
    monkeypatch.setattr(
        editor_mod, "run_ffmpeg_command", lambda cmd: mock.Mock(returncode=0, stderr="")
    )

    messages: list[str] = []
    editor_mod.run_edit(session.folder, out=messages.append)
    assert any("timelapse" in m.lower() for m in messages)


def test_run_edit_propagates_ffmpeg_failure(tmp_path, monkeypatch):
    from benchcam import session as session_mod

    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mp4").write_bytes(b"video")

    monkeypatch.setattr(editor_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(editor_mod, "probe_duration", lambda *a, **k: 30.0)
    monkeypatch.setattr(editor_mod, "probe_has_audio", lambda *a, **k: True)
    monkeypatch.setattr(
        editor_mod,
        "run_ffmpeg_command",
        lambda cmd: mock.Mock(returncode=1, stderr="boom: bad filtergraph"),
    )

    with pytest.raises(EditError) as exc:
        editor_mod.run_edit(session.folder, out=lambda _m: None)
    assert "ffmpeg failed" in str(exc.value).lower()
