"""Tests for the local web dashboard.

The controller logic is tested directly (mocking only the recorder and the edit
render), and one end-to-end HTTP test exercises the stdlib server with urllib.
No real browser, OBS, or encode is used.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

from benchcam import config as config_mod
from benchcam import dashboard as dash
from benchcam import editor as editor_mod
from benchcam import session as session_mod
from benchcam.dashboard import DashboardController, DashboardError, make_server
from benchcam.markers import read_markers
from benchcam.recorders.base import RecorderError


class FakeRecorder:
    """Stand-in recorder that records start/stop without touching hardware."""

    def __init__(self, *, fail_start=False):
        self.fail_start = fail_start
        self.start_calls = 0
        self.stop_calls = 0

    def start(self, storage_path):
        self.start_calls += 1
        if self.fail_start:
            raise RecorderError("OBS is not running")

    def stop(self):
        self.stop_calls += 1


@pytest.fixture
def root(tmp_path):
    return tmp_path / "sessions"


def _use_recorder(monkeypatch, recorder):
    monkeypatch.setattr(dash, "get_recorder", lambda name: recorder)


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #

def test_start_creates_and_starts_session(monkeypatch, root):
    rec = FakeRecorder()
    _use_recorder(monkeypatch, rec)
    ctrl = DashboardController(root)

    status = ctrl.start("obs", "bench-a")

    assert status["active"] is True
    assert status["recorder"] == "obs"
    assert rec.start_calls == 1
    # Real session was created + started on disk.
    session = session_mod.get_active_session(root)
    assert session.profile == "bench-a"
    assert session.status == session_mod.STATUS_RUNNING


def test_double_start_is_refused(monkeypatch, root):
    _use_recorder(monkeypatch, FakeRecorder())
    ctrl = DashboardController(root)
    ctrl.start("obs", "p")
    with pytest.raises(DashboardError):
        ctrl.start("obs", "p")


def test_start_surfaces_recorder_error_and_stays_inactive(monkeypatch, root):
    _use_recorder(monkeypatch, FakeRecorder(fail_start=True))
    ctrl = DashboardController(root)
    with pytest.raises(RecorderError):
        ctrl.start("obs", "p")
    assert ctrl.status()["active"] is False  # no half-started session left active


def test_mark_calls_add_marker(monkeypatch, root):
    _use_recorder(monkeypatch, FakeRecorder())
    ctrl = DashboardController(root)
    ctrl.start("obs", "p")

    result = ctrl.mark("power on")
    ctrl.mark("")  # unlabeled

    assert result["marker"]["index"] == 1
    assert result["marker"]["label"] == "power on"
    session = session_mod.get_active_session(root)
    rows = read_markers(session.markers_file)
    assert [r["label"] for r in rows] == ["power on", ""]
    assert ctrl.status()["marker_count"] == 2


def test_mark_without_active_session_raises(root):
    ctrl = DashboardController(root)
    with pytest.raises(DashboardError):
        ctrl.mark("x")


def test_note_appends_to_notes(monkeypatch, root):
    _use_recorder(monkeypatch, FakeRecorder())
    ctrl = DashboardController(root)
    ctrl.start("obs", "p")
    ctrl.note("voltage looked off")
    session = session_mod.get_active_session(root)
    assert "voltage looked off" in session.notes_file.read_text(encoding="utf-8")


def test_stop_ends_session_and_stops_recorder(monkeypatch, root):
    rec = FakeRecorder()
    _use_recorder(monkeypatch, rec)
    ctrl = DashboardController(root)
    started = ctrl.start("obs", "p")
    folder = started["folder"]

    result = ctrl.stop()

    assert result["stopped"] is True
    assert rec.stop_calls == 1
    assert result["summary"]["folder"] == folder
    # Session ended on disk; controller is idle again.
    reloaded = session_mod.load_session(folder)
    assert reloaded.status == session_mod.STATUS_ENDED
    assert ctrl.status()["active"] is False


def test_stop_when_inactive_is_a_clear_noop(root):
    ctrl = DashboardController(root)
    result = ctrl.stop()
    assert result["stopped"] is False
    assert "No active session" in result["message"]


def test_stop_is_robust_when_recorder_already_stopped(monkeypatch, root):
    # Simulate OBS already stopped manually: recorder.stop() raises. The session
    # must still end cleanly (UI leaves RECORDING) and the error is surfaced.
    class FailingStop(FakeRecorder):
        def stop(self):
            self.stop_calls += 1
            raise RecorderError("output not active")

    rec = FailingStop()
    _use_recorder(monkeypatch, rec)
    ctrl = DashboardController(root)
    started = ctrl.start("obs", "p")

    result = ctrl.stop()

    assert result["stopped"] is True
    assert "warning" in result and result["warning"]
    assert rec.stop_calls == 1
    assert ctrl.status()["active"] is False  # never stuck "RECORDING"
    reloaded = session_mod.load_session(started["folder"])
    assert reloaded.status == session_mod.STATUS_ENDED
    assert reloaded.ended_wall_time is not None


# --------------------------------------------------------------------------- #
# Label-after-mark
# --------------------------------------------------------------------------- #

def test_label_marker_updates_existing_marker_in_csv(monkeypatch, root):
    _use_recorder(monkeypatch, FakeRecorder())
    ctrl = DashboardController(root)
    ctrl.start("obs", "p")
    ctrl.mark("")  # marker #1, instant, no label
    ctrl.mark("")  # marker #2, instant, no label

    res = ctrl.label_marker(2, "chip lifted")
    assert res["labeled"] == 2

    session = session_mod.get_active_session(root)
    rows = read_markers(session.markers_file)
    assert rows[1]["label"] == "chip lifted"
    assert rows[0]["label"] == ""
    assert rows[1]["source"] == "manual"  # other fields preserved

    # Editing the label again works.
    ctrl.label_marker(2, "chip lifted cleanly")
    rows = read_markers(session.markers_file)
    assert rows[1]["label"] == "chip lifted cleanly"


def test_label_marker_unknown_index_raises(monkeypatch, root):
    _use_recorder(monkeypatch, FakeRecorder())
    ctrl = DashboardController(root)
    ctrl.start("obs", "p")
    ctrl.mark("")
    with pytest.raises(DashboardError):
        ctrl.label_marker(99, "nope")


def test_review_calls_edit_on_last_session(monkeypatch, root):
    _use_recorder(monkeypatch, FakeRecorder())
    ctrl = DashboardController(root)
    started = ctrl.start("obs", "p")
    ctrl.stop()

    captured = {}

    def fake_run_edit(session_dir, *, pre, post, speed, out):
        captured["dir"] = str(session_dir)
        captured["params"] = (pre, post, speed)
        return f"{session_dir}/review.mp4"

    monkeypatch.setattr(editor_mod, "run_edit", fake_run_edit)

    result = ctrl.review(pre=2.0, post=4.0, speed=10.0)

    assert captured["dir"] == started["folder"]
    assert captured["params"] == (2.0, 4.0, 10.0)
    assert result["review_path"].endswith("review.mp4")


def test_review_without_finished_session_raises(root):
    ctrl = DashboardController(root)
    with pytest.raises(DashboardError):
        ctrl.review()


def test_review_auto_opens_the_clip(monkeypatch, root):
    # Fix 4: review() opens the rendered file in the default player.
    _use_recorder(monkeypatch, FakeRecorder())
    ctrl = DashboardController(root)
    started = ctrl.start("obs", "p")
    ctrl.stop()

    monkeypatch.setattr(editor_mod, "run_edit", lambda d, **k: f"{d}/review.mp4")
    opened = {}
    monkeypatch.setattr(dash, "open_file", lambda p: opened.setdefault("path", str(p)))

    res = ctrl.review()

    assert res["opened"] is True
    assert opened["path"].endswith("review.mp4")
    assert res["review_path"].endswith("review.mp4")  # green link still returned


def test_review_open_failure_is_nonfatal_and_keeps_link(monkeypatch, root):
    _use_recorder(monkeypatch, FakeRecorder())
    ctrl = DashboardController(root)
    ctrl.start("obs", "p")
    ctrl.stop()

    monkeypatch.setattr(editor_mod, "run_edit", lambda d, **k: f"{d}/review.mp4")

    def boom(_p):
        raise OSError("no media player")

    monkeypatch.setattr(dash, "open_file", boom)

    res = ctrl.review()  # must not raise
    assert res["opened"] is False
    assert res["review_path"].endswith("review.mp4")


# --------------------------------------------------------------------------- #
# Page markup (Fixes 2 & 3 are client-side; assert the served HTML/JS)
# --------------------------------------------------------------------------- #

def test_page_uses_inpage_confirm_not_native_dialogs():
    html = dash.PAGE_HTML
    assert "stopConfirm" in html and "Confirm stop" in html
    assert "to confirm" in html and "to cancel" in html
    # No native browser dialogs anywhere in the flow.
    assert "window.confirm" not in html
    assert "confirm(" not in html
    assert "alert(" not in html


def test_page_note_and_label_blur_on_enter():
    html = dash.PAGE_HTML
    # Both the label and note fields submit-and-exit (blur) on Enter (Fix 2).
    assert html.count("e.target.blur()") >= 2


def test_page_keeps_keyboard_legend_and_shortcuts():
    html = dash.PAGE_HTML
    assert "mark now" in html  # legend present
    assert "doMark" in html and "focusLastLabel" in html and "requestStop" in html


# --------------------------------------------------------------------------- #
# Cleanup 1 — idempotent launch (reuse a running dashboard)
# --------------------------------------------------------------------------- #

def test_is_dashboard_running_detects_serving_and_closed(tmp_path):
    import socket

    httpd, _ = make_server("127.0.0.1", 0, tmp_path / "sessions")
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert dash.is_dashboard_running("127.0.0.1", port) is True
    finally:
        httpd.shutdown()
        httpd.server_close()

    # A port with nothing listening must report False.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()
    assert dash.is_dashboard_running("127.0.0.1", free_port) is False


def test_serve_reuses_running_dashboard_instead_of_starting_a_second(monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "is_dashboard_running", lambda *a, **k: True)
    opened = {}
    monkeypatch.setattr(dash.webbrowser, "open", lambda url: opened.setdefault("url", url))

    def must_not_start(*a, **k):
        raise AssertionError("serve() must not start a second server when one is running")

    monkeypatch.setattr(dash, "make_server", must_not_start)

    rc = dash.serve(host="127.0.0.1", port=8765, sessions_root=tmp_path, open_browser=True)

    assert rc == 0
    assert opened["url"] == "http://127.0.0.1:8765/"


# --------------------------------------------------------------------------- #
# Cleanup 2 — hide the password field when one is saved
# --------------------------------------------------------------------------- #

def test_build_page_includes_password_field_when_not_saved():
    html = dash.build_page(has_password=False)
    assert 'id="obsPassword"' in html


def test_build_page_omits_password_field_when_saved():
    html = dash.build_page(has_password=True)
    assert 'id="obsPassword"' not in html
    assert "OBS password saved" in html
    assert "changePw" in html  # the (change) reveal link


def test_render_page_reflects_saved_password(tmp_path):
    ctrl = DashboardController(tmp_path / "sessions", config_root=tmp_path)
    assert 'id="obsPassword"' in ctrl.render_page()  # nothing saved yet
    config_mod.save_config({"obs": {"password": "x"}}, tmp_path)
    assert 'id="obsPassword"' not in ctrl.render_page()  # field hidden once saved


# --------------------------------------------------------------------------- #
# OBS connection settings (config file)
# --------------------------------------------------------------------------- #

def test_obs_password_resolves_from_config_when_env_absent(monkeypatch, tmp_path):
    # No global env var (deleting also makes monkeypatch restore/clean up after).
    monkeypatch.delenv("BENCHCAM_OBS_PASSWORD", raising=False)
    monkeypatch.delenv("BENCHCAM_OBS_HOST", raising=False)
    monkeypatch.delenv("BENCHCAM_OBS_PORT", raising=False)
    config_mod.save_config({"obs": {"password": "sekret", "port": 4499}}, tmp_path)

    _use_recorder(monkeypatch, FakeRecorder())
    ctrl = DashboardController(tmp_path / "sessions", config_root=tmp_path)
    ctrl.start("obs", "p")

    # The saved config password/port are applied where ObsRecorder reads them.
    assert os.environ["BENCHCAM_OBS_PASSWORD"] == "sekret"
    assert os.environ["BENCHCAM_OBS_PORT"] == "4499"


def test_explicit_password_is_persisted_and_overrides_config(monkeypatch, tmp_path):
    monkeypatch.delenv("BENCHCAM_OBS_PASSWORD", raising=False)
    config_mod.save_config({"obs": {"password": "old"}}, tmp_path)

    _use_recorder(monkeypatch, FakeRecorder())
    ctrl = DashboardController(tmp_path / "sessions", config_root=tmp_path)
    ctrl.start("obs", "p", obs_password="fresh", obs_host="10.0.0.5", obs_port="4500")

    saved = config_mod.load_config(tmp_path)["obs"]
    assert saved["password"] == "fresh"
    assert saved["host"] == "10.0.0.5"
    assert saved["port"] == 4500
    assert os.environ["BENCHCAM_OBS_PASSWORD"] == "fresh"


def test_saved_password_is_reused_without_entry(monkeypatch, tmp_path):
    # Fix 1: with a password saved in config, Start connects without me typing it.
    monkeypatch.delenv("BENCHCAM_OBS_PASSWORD", raising=False)
    config_mod.save_config({"obs": {"password": "sekret"}}, tmp_path)

    seen = {}

    class CapturingRecorder(FakeRecorder):
        def start(self, storage_path):
            super().start(storage_path)
            seen["pw"] = os.environ.get("BENCHCAM_OBS_PASSWORD")

    _use_recorder(monkeypatch, CapturingRecorder())
    ctrl = DashboardController(tmp_path / "sessions", config_root=tmp_path)
    ctrl.start("obs", "p")  # no obs_password provided

    assert seen["pw"] == "sekret"


def test_default_config_root_is_stable_and_in_repo():
    # Fix 1: config path doesn't depend on the cwd, so it's the same every launch.
    root = config_mod.default_config_root()
    assert (root / "pyproject.toml").exists()
    assert config_mod.config_path() == root / ".benchcam" / "config.json"


def test_get_config_never_returns_password(tmp_path):
    config_mod.save_config({"obs": {"password": "sekret", "host": "h", "port": 4455}}, tmp_path)
    ctrl = DashboardController(tmp_path / "sessions", config_root=tmp_path)
    obs = ctrl.get_config()["config"]["obs"]
    assert obs["has_password"] is True
    assert obs["host"] == "h"
    assert obs["port"] == 4455
    assert "password" not in obs


def test_config_file_location_and_roundtrip(tmp_path):
    path = config_mod.save_config({"obs": {"password": "x"}}, tmp_path)
    assert path == tmp_path / ".benchcam" / "config.json"
    assert config_mod.load_config(tmp_path)["obs"]["password"] == "x"


def test_config_dir_is_gitignored():
    gitignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(
        encoding="utf-8"
    )
    assert ".benchcam/" in gitignore


# --------------------------------------------------------------------------- #
# HTTP layer (real stdlib server, mocked recorder)
# --------------------------------------------------------------------------- #

def _post(port, path, payload):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as resp:
        return resp.read()


def test_http_endpoints_drive_a_full_session(monkeypatch, root):
    _use_recorder(monkeypatch, FakeRecorder())
    httpd, _ = make_server("127.0.0.1", 0, root)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        # The page is served at "/".
        assert b"BenchCam" in _get(port, "/")

        # Initially idle.
        assert json.loads(_get(port, "/api/status"))["active"] is False

        # start -> mark -> stop.
        started = _post(port, "/api/start", {"recorder": "obs", "profile": "p"})
        assert started["ok"] is True and started["active"] is True

        # double-start refused over HTTP.
        again = _post(port, "/api/start", {"recorder": "obs"})
        assert again["ok"] is False and "already active" in again["error"]

        marked = _post(port, "/api/mark", {"label": ""})
        assert marked["ok"] is True and marked["marker"]["index"] == 1

        # Label the marker after the fact.
        labeled = _post(port, "/api/label", {"index": 1, "label": "power on"})
        assert labeled["ok"] is True and labeled["labeled"] == 1
        status = json.loads(_get(port, "/api/status"))
        assert status["markers"][0]["label"] == "power on"

        stopped = _post(port, "/api/stop", {})
        assert stopped["ok"] is True and stopped["stopped"] is True

        # stop again -> clear no-op, not an error.
        again_stop = _post(port, "/api/stop", {})
        assert again_stop["ok"] is True and again_stop["stopped"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()
