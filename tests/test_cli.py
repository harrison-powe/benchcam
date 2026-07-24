"""End-to-end tests for the CLI flow (new -> run -> mark -> end)."""

from __future__ import annotations

import argparse
from pathlib import Path

from benchcam import autochapter as autochapter_mod
from benchcam import cli as cli_mod
from benchcam import dashboard as dashboard_mod
from benchcam import editor as editor_mod
from benchcam import label as label_mod
from benchcam import session as session_mod
from benchcam import transcribe as transcribe_mod
from benchcam.cli import cmd_fetch, main
from benchcam.markers import Marker, append_marker, read_markers
from benchcam.transcribe import TranscriptSegment as TS


def test_full_cli_flow(tmp_path, capsys):
    root = str(tmp_path / "sessions")

    assert main(["new", "--sessions-root", root, "--profile", "demo"]) == 0
    assert main(["run", "--sessions-root", root]) == 0
    assert main(["mark", "--sessions-root", root, "power on"]) == 0
    assert main(["mark", "--sessions-root", root, "fault", "--source", "external"]) == 0
    assert main(["end", "--sessions-root", root]) == 0

    session = session_mod.get_active_session(tmp_path / "sessions")
    assert session.status == session_mod.STATUS_ENDED

    rows = read_markers(session.markers_file)
    assert [r["label"] for r in rows] == ["power on", "fault"]
    assert rows[1]["source"] == "external"


def test_new_persists_free_space_floor_knobs_to_session_json(tmp_path):
    # The pre-flight free-space knobs set on 'new' must land in session.json so
    # every start path (run/live/dashboard/GPIO START) resolves the same floor.
    root = tmp_path / "sessions"
    assert main([
        "new", "--sessions-root", str(root),
        "--min-session-minutes", "30",
        "--capture-rate-mb-min", "700",
    ]) == 0

    session = session_mod.get_active_session(root)
    assert session.min_session_minutes == 30.0
    assert session.capture_rate_mb_per_min == 700.0
    # Unset by default (falls back to env/default at start time).
    plain = session_mod.create_session(root=root)
    assert plain.min_session_minutes is None
    assert plain.capture_rate_mb_per_min is None


def test_mark_without_session_returns_error(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    rc = main(["mark", "--sessions-root", root, "oops"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No active session" in err


def test_sessions_root_env_var_is_default_when_flag_omitted(tmp_path, monkeypatch):
    # Setting BENCHCAM_SESSIONS_ROOT (e.g. to an external SSD path) makes new
    # sessions land there without passing --sessions-root.
    root = tmp_path / "ssd" / "benchcam-sessions"
    monkeypatch.setenv("BENCHCAM_SESSIONS_ROOT", str(root))

    assert main(["new", "--profile", "ssd-test"]) == 0

    active = session_mod.get_active_session(root)
    assert active.profile == "ssd-test"
    assert str(root) in active.storage_path


def test_run_with_obs_reports_clear_error_without_extra(tmp_path, capsys):
    # Without the optional obsws-python extra installed (or without OBS
    # running), selecting the obs recorder must fail cleanly with an actionable
    # message rather than silently falling back to null.
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root, "--recorder", "obs"]) == 0
    rc = main(["run", "--sessions-root", root])
    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "obs" in err


def test_dashboard_lan_flag_binds_to_all_interfaces(tmp_path, monkeypatch):
    # --lan must expose on the LAN (0.0.0.0); default stays localhost-only.
    captured = {}
    monkeypatch.setattr(
        dashboard_mod, "serve",
        lambda **kw: captured.update(kw) or 0,
    )
    rc = main(["dashboard", "--sessions-root", str(tmp_path), "--lan", "--no-browser"])
    assert rc == 0
    assert captured["host"] == "0.0.0.0"
    assert captured["open_browser"] is False


def test_dashboard_defaults_to_localhost(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.delenv(dashboard_mod.ENV_DASHBOARD_HOST, raising=False)
    monkeypatch.setattr(dashboard_mod, "serve", lambda **kw: captured.update(kw) or 0)
    rc = main(["dashboard", "--sessions-root", str(tmp_path), "--no-browser"])
    assert rc == 0
    assert captured["host"] == "127.0.0.1"


def test_dashboard_host_env_var_sets_bind_host(tmp_path, monkeypatch):
    monkeypatch.setenv(dashboard_mod.ENV_DASHBOARD_HOST, "0.0.0.0")
    captured = {}
    monkeypatch.setattr(dashboard_mod, "serve", lambda **kw: captured.update(kw) or 0)
    rc = main(["dashboard", "--sessions-root", str(tmp_path), "--no-browser"])
    assert rc == 0
    assert captured["host"] == "0.0.0.0"


def test_fetch_invokes_scp_with_remote_and_dest(tmp_path, monkeypatch):
    # fetch must scp the right "host:remote-root/<session>" to the local
    # sessions root, without performing a real copy or opening anything.
    session_id = "2026-06-23_20-17-17"
    root = tmp_path / "sessions"
    recorded = {}

    def fake_run(argv, *args, **kwargs):
        recorded["argv"] = argv
        return argparse.Namespace(returncode=0)

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cli_mod.os, "startfile", lambda *a, **k: None, raising=False)

    args = argparse.Namespace(
        sessions_root=str(root),
        session=session_id,
        host="harrison@tatooine.local",
        remote_root="/home/harrison/benchcam/sessions",
        no_open=False,
    )
    rc = cmd_fetch(args)
    assert rc == 0

    argv = recorded["argv"]
    assert argv[0] == "scp"
    assert (
        f"harrison@tatooine.local:/home/harrison/benchcam/sessions/{session_id}"
        in argv
    )
    assert str(root) in argv


# --------------------------------------------------------------------------- #
# edit --auto orchestration (transcribe + autochapter, skip-if-cached) & --preview
# --------------------------------------------------------------------------- #

# Two transcript segments and a Claude payload that cites them verbatim, so the
# validated chapters land at 10s and 300s (far from the real markers seeded below).
_SEGS = [
    TS(10, 15, "okay starting the stiffness test now"),
    TS(300, 306, "and now the velocity sweep is running"),
]
_PAYLOAD = (
    '[{"segment": 0, "quote": "starting the stiffness test", "at_seconds": 10, '
    '"title": "Stiffness Test"},'
    ' {"segment": 1, "quote": "velocity sweep is running", "at_seconds": 300, '
    '"title": "Velocity Sweep"}]'
)


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


def _mock_auto_pipeline(monkeypatch):
    """Mock Whisper + the Anthropic clients across the --auto pipeline, and stub
    the render.

    The pipeline is transcribe -> label -> autochapter -> render, so Whisper and
    capture probing are patched on BOTH transcribe and autochapter (separate
    name bindings of the same functions), and label gets its own fake client
    (returns "Test Label") with its own counter. Returns the call counters.
    """
    # Pin the Whisper-model resolution: cache reuse is provenance-checked, so a
    # stray $BENCHCAM_WHISPER_MODEL would turn the model="small" cache into a miss.
    monkeypatch.delenv("BENCHCAM_WHISPER_MODEL", raising=False)
    calls = {"transcribe": 0, "api": 0, "label_api": 0, "render": 0}

    def fake_transcribe(capture, model, *, language="en"):
        calls["transcribe"] += 1
        return _SEGS

    class _Msgs:
        def create(self, **kwargs):
            calls["api"] += 1
            return _Resp(_PAYLOAD)

    class _Client:
        messages = _Msgs()

    class _LabelMsgs:
        def create(self, **kwargs):
            calls["label_api"] += 1
            return _Resp("Test Label")

    class _LabelClient:
        messages = _LabelMsgs()

    monkeypatch.setattr(autochapter_mod, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(autochapter_mod, "probe_has_audio", lambda *a, **k: True)
    monkeypatch.setattr(autochapter_mod, "find_capture", lambda d: Path(d) / "capture.mkv")
    monkeypatch.setattr(autochapter_mod.shutil, "which", lambda _n: "/usr/bin/x")
    monkeypatch.setattr(autochapter_mod, "make_client", lambda: _Client())
    monkeypatch.setattr(transcribe_mod, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(transcribe_mod, "probe_has_audio", lambda *a, **k: True)
    monkeypatch.setattr(transcribe_mod, "find_capture", lambda d: Path(d) / "capture.mkv")
    monkeypatch.setattr(label_mod, "make_client", lambda: _LabelClient())

    def fake_render(session_dir, **kwargs):
        calls["render"] += 1
        return Path(session_dir) / "review.mp4"

    monkeypatch.setattr(editor_mod, "run_edit", fake_render)
    return calls


def test_edit_auto_skips_transcribe_when_transcript_exists(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mkv").write_bytes(b"video")
    # A cached transcript exists; no source=auto markers yet.
    autochapter_mod.save_transcript(session.folder, _SEGS, model="small", language="en")

    calls = _mock_auto_pipeline(monkeypatch)
    rc = main(["edit", "--sessions-root", str(root), "--session", session.session_id, "--auto"])

    assert rc == 0
    assert calls["transcribe"] == 0  # transcript.json reused -> no Whisper
    assert calls["api"] == 1  # autochapter still proposed chapters
    assert calls["render"] == 1  # then rendered
    auto = [r for r in read_markers(session.markers_file) if r["source"] == "auto"]
    assert [r["label"] for r in auto] == ["Stiffness Test", "Velocity Sweep"]


def test_edit_auto_skips_autochapter_when_auto_markers_exist(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mkv").write_bytes(b"video")
    # A prior source=auto chapter already exists (as after a first --auto run, or a
    # title edit that keeps the source=auto cell), and the transcript is cached —
    # the fully-processed state, which must cost zero Whisper/API before render.
    autochapter_mod.save_transcript(session.folder, _SEGS, model="small", language="en")
    append_marker(session.markers_file, Marker(1, 42.0, "", "auto", "Prior Chapter"))

    calls = _mock_auto_pipeline(monkeypatch)
    rc = main(["edit", "--sessions-root", str(root), "--session", session.session_id, "--auto"])

    assert rc == 0
    assert calls["transcribe"] == 0  # autochapter skipped whole...
    assert calls["api"] == 0  # ...no Whisper AND no API call
    assert calls["render"] == 1  # straight to render
    # The existing auto chapter is untouched (not regenerated).
    auto = [r for r in read_markers(session.markers_file) if r["source"] == "auto"]
    assert [r["label"] for r in auto] == ["Prior Chapter"]


def test_edit_auto_runs_both_when_neither_exists(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mkv").write_bytes(b"video")
    # Neither a cached transcript nor any source=auto markers.

    calls = _mock_auto_pipeline(monkeypatch)
    rc = main(["edit", "--sessions-root", str(root), "--session", session.session_id, "--auto"])

    assert rc == 0
    assert calls["transcribe"] == 1  # full-session transcription ran...
    assert calls["api"] == 1  # ...and chapters were proposed
    assert calls["render"] == 1
    assert autochapter_mod.transcript_path(session.folder).exists()  # cached
    auto = [r for r in read_markers(session.markers_file) if r["source"] == "auto"]
    assert [r["label"] for r in auto] == ["Stiffness Test", "Velocity Sweep"]


def test_edit_auto_fills_narration_and_labels_empty_gpio_markers(tmp_path, monkeypatch):
    # THE defect the pipeline closes: a gpio press arrives with an empty label;
    # --auto must give it narration from its own +/-window and an AI label, so
    # the press renders with text AND its conflict-window suppression of the
    # nearby auto proposal becomes correct (the real press carries content).
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mkv").write_bytes(b"video")
    append_marker(session.markers_file, Marker(1, 12.0, "", "gpio", ""))

    calls = _mock_auto_pipeline(monkeypatch)
    rc = main(["edit", "--sessions-root", str(root), "--session", session.session_id, "--auto"])

    assert rc == 0
    assert calls["transcribe"] == 1 and calls["label_api"] == 1 and calls["api"] == 1
    rows = read_markers(session.markers_file)
    gpio = next(r for r in rows if "gpio" in r["source"].split("+"))
    assert gpio["narration"] == "okay starting the stiffness test now"  # +/-10 of 12s
    assert gpio["label"] == "Test Label"
    assert gpio["source"] == "gpio+transcribed+ai-labeled"
    # The 10s proposal is suppressed by the (now labeled) press at 12s; only the
    # far chapter survives — suppression now removes a duplicate, not the title.
    auto = [r for r in rows if r["source"] == "auto"]
    assert [r["label"] for r in auto] == ["Velocity Sweep"]


def test_edit_auto_overwrite_auto_runs_exactly_one_whisper_pass(tmp_path, monkeypatch):
    # Regression guard for the double-transcription defect: --overwrite-auto
    # forces the fresh pass at step 1 (run_transcribe) and autochapter must then
    # REUSE it via the shared cache — one pass total, chapters and narration
    # from the SAME transcript. A valid cache exists, so the pass must happen
    # exactly once (not zero: forced fresh; not two: shared).
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mkv").write_bytes(b"video")
    autochapter_mod.save_transcript(session.folder, _SEGS, model="small", language="en")
    append_marker(session.markers_file, Marker(1, 500.0, "", "auto", "Stale Auto"))

    calls = _mock_auto_pipeline(monkeypatch)
    rc = main([
        "edit", "--sessions-root", str(root), "--session", session.session_id,
        "--auto", "--overwrite-auto",
    ])

    assert rc == 0
    assert calls["transcribe"] == 1  # exactly one pass, at step 1
    assert calls["api"] == 1  # chapters regenerated from that same transcript
    auto = [r["label"] for r in read_markers(session.markers_file) if r["source"] == "auto"]
    assert auto == ["Stiffness Test", "Velocity Sweep"]  # stale row replaced


def test_edit_auto_never_touches_hand_written_labels_or_narration(tmp_path, monkeypatch):
    # Invariants: narration AND label stay fill-empty-only, even under
    # --overwrite-auto. Hand titles and hand-rebuilt narration must survive.
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mkv").write_bytes(b"video")
    # Hand label + empty narration: narration may fill, the label must not move.
    append_marker(session.markers_file, Marker(1, 12.0, "", "gpio", "HAND TITLE"))
    # Hand narration + empty label: narration must not move, label fills FROM it.
    append_marker(
        session.markers_file, Marker(2, 302.0, "", "gpio", "", "my hand narration")
    )

    calls = _mock_auto_pipeline(monkeypatch)
    rc = main([
        "edit", "--sessions-root", str(root), "--session", session.session_id,
        "--auto", "--overwrite-auto",
    ])

    assert rc == 0
    rows = {int(r["marker_index"]): r for r in read_markers(session.markers_file)}
    assert rows[1]["label"] == "HAND TITLE"  # never clobbered
    assert rows[1]["narration"] == "okay starting the stiffness test now"
    assert rows[2]["narration"] == "my hand narration"  # fill-empty-only
    assert rows[2]["label"] == "Test Label"  # filled FROM the hand narration


def test_edit_overwrite_auto_preserves_gpio_and_manual_markers(tmp_path, monkeypatch):
    # --overwrite-auto is the one path that re-triggers regeneration from inside
    # edit: it must replace ONLY prior source=auto rows and never touch a real
    # button-press (gpio) or hand-placed (manual) marker.
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mkv").write_bytes(b"video")
    append_marker(session.markers_file, Marker(1, 1000.0, "w1", "gpio", "Button Press"))
    append_marker(session.markers_file, Marker(2, 2000.0, "w2", "manual", "Hand Note"))
    append_marker(session.markers_file, Marker(3, 500.0, "", "auto", "Stale Auto"))

    calls = _mock_auto_pipeline(monkeypatch)
    rc = main([
        "edit", "--sessions-root", str(root), "--session", session.session_id,
        "--auto", "--overwrite-auto",
    ])

    assert rc == 0
    assert calls["transcribe"] == 1  # regeneration forced despite existing auto rows
    rows = read_markers(session.markers_file)
    by_source = {r["source"]: r for r in rows}

    # Real markers preserved verbatim: same time, label, source — none deleted.
    gpio = next(r for r in rows if r["source"] == "gpio")
    manual = next(r for r in rows if r["source"] == "manual")
    assert gpio["label"] == "Button Press" and gpio["elapsed_seconds"] == "1000.000"
    assert manual["label"] == "Hand Note" and manual["elapsed_seconds"] == "2000.000"

    # Prior auto row replaced (not merely appended to): the stale one is gone,
    # the freshly proposed chapters are present, all still source=auto.
    auto_labels = [r["label"] for r in rows if r["source"] == "auto"]
    assert "Stale Auto" not in auto_labels
    assert sorted(auto_labels) == ["Stiffness Test", "Velocity Sweep"]


# --------------------------------------------------------------------------- #
# edit --fetch: pull-if-missing prefix step to the render
# --------------------------------------------------------------------------- #

_FETCH_SID = "2026-07-05_10-00-00"


def _fake_scp_creates_session(root, sid, recorder):
    """A subprocess.run stand-in that simulates 'scp -r' pulling the session."""
    def fake_run(argv, *args, **kwargs):
        recorder.append(argv)
        d = root / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "session.json").write_text("{}", encoding="utf-8")
        (d / "capture.mkv").write_bytes(b"v")
        return argparse.Namespace(returncode=0)
    return fake_run


def _stub_render(monkeypatch, sink):
    def fake_render(session_dir, **kwargs):
        sink.append(Path(session_dir))
        return Path(session_dir) / "review.mp4"
    monkeypatch.setattr(editor_mod, "run_edit", fake_render)


def test_edit_fetch_pulls_when_missing_then_edits(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    scp, rendered, opened = [], [], []
    monkeypatch.setattr(cli_mod.subprocess, "run", _fake_scp_creates_session(root, _FETCH_SID, scp))
    monkeypatch.setattr(cli_mod.os, "startfile", lambda *a, **k: opened.append(a), raising=False)
    _stub_render(monkeypatch, rendered)

    rc = main(["edit", "--sessions-root", str(root), "--session", _FETCH_SID, "--fetch"])

    assert rc == 0
    # scp pulled the right remote into the local sessions root...
    assert len(scp) == 1
    argv = scp[0]
    assert argv[0] == "scp" and argv[1] == "-r"
    assert f"harrison@tatooine.local:/home/harrison/benchcam/sessions/{_FETCH_SID}" in argv
    assert str(root) in argv
    # ...then edited the fetched session.
    assert rendered == [root / _FETCH_SID]
    # The standalone fetch folder/VLC auto-open must NOT fire mid-pipeline.
    assert opened == []


def test_edit_fetch_skips_when_already_local(tmp_path, monkeypatch, capsys):
    root = tmp_path / "sessions"
    d = root / _FETCH_SID
    d.mkdir(parents=True)
    (d / "session.json").write_text("{}", encoding="utf-8")
    (d / "capture.mkv").write_bytes(b"v")  # capture present -> already local

    scp, rendered = [], []
    monkeypatch.setattr(cli_mod.subprocess, "run", lambda *a, **k: scp.append(a) or argparse.Namespace(returncode=0))
    _stub_render(monkeypatch, rendered)

    rc = main(["edit", "--sessions-root", str(root), "--session", _FETCH_SID, "--fetch"])

    assert rc == 0
    assert scp == []  # no download
    assert rendered == [d]
    assert "already local" in capsys.readouterr().out


def test_edit_fetch_composes_with_auto(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    scp, auto, rendered = [], [], []
    monkeypatch.setattr(cli_mod.subprocess, "run", _fake_scp_creates_session(root, _FETCH_SID, scp))
    monkeypatch.setattr(cli_mod.os, "startfile", lambda *a, **k: None, raising=False)
    # This test asserts fetch -> pipeline -> render ORDERING, not pipeline
    # internals (covered elsewhere): stub all three pipeline steps.
    monkeypatch.setattr(transcribe_mod, "run_transcribe", lambda sd, **k: [])
    monkeypatch.setattr(label_mod, "run_label", lambda sd, **k: [])
    monkeypatch.setattr(autochapter_mod, "run_autochapter", lambda sd, **k: auto.append(Path(sd)))
    _stub_render(monkeypatch, rendered)

    rc = main([
        "edit", "--sessions-root", str(root), "--session", _FETCH_SID, "--fetch", "--auto",
    ])

    assert rc == 0
    assert len(scp) == 1  # fetched first
    assert auto == [root / _FETCH_SID]  # then autochapter ran
    assert rendered == [root / _FETCH_SID]  # then rendered


def test_edit_fetch_requires_session(tmp_path, monkeypatch, capsys):
    root = tmp_path / "sessions"
    scp = []
    monkeypatch.setattr(cli_mod.subprocess, "run", lambda *a, **k: scp.append(a) or argparse.Namespace(returncode=0))

    rc = main(["edit", "--sessions-root", str(root), "--fetch"])  # no --session

    assert rc == 1
    assert scp == []  # never attempted a transfer
    err = capsys.readouterr().err
    assert "--fetch" in err and "--session" in err


def test_plain_edit_without_fetch_still_requires_local(tmp_path, monkeypatch, capsys):
    root = tmp_path / "sessions"
    scp = []
    monkeypatch.setattr(cli_mod.subprocess, "run", lambda *a, **k: scp.append(a) or argparse.Namespace(returncode=0))

    rc = main(["edit", "--sessions-root", str(root), "--session", "2026-missing"])

    assert rc == 1  # errors as today when the session isn't local
    assert scp == []  # plain edit never fetches
    assert "not found" in capsys.readouterr().err.lower()


def test_end_on_stale_active_pointer_no_ops_gracefully(tmp_path, capsys):
    # A dangling .active must not wedge `benchcam end`: it reports "no active
    # session" (exit 1, no traceback), warns that it cleared the stale pointer,
    # and leaves the pointer gone so the next `new` works.
    import shutil

    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root]) == 0
    active = session_mod.get_active_session(tmp_path / "sessions")
    shutil.rmtree(active.folder)  # delete the folder out from under the pointer

    rc = main(["end", "--sessions-root", root])

    assert rc == 1  # nothing active -> clean non-zero, not a crash
    err = capsys.readouterr().err
    assert "No active session" in err               # the graceful message
    assert "folder is missing" not in err           # not the old dead-end wedge
    assert "stale active-session pointer" in err     # self-heal trace
    assert "Traceback" not in err                    # did not crash
    assert not session_mod._active_pointer(tmp_path / "sessions").exists()
