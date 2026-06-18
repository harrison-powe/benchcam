"""Tests for the local web dashboard.

The controller logic is tested directly (mocking only the recorder and the edit
render), and one end-to-end HTTP test exercises the stdlib server with urllib.
No real browser, OBS, or encode is used.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from unittest import mock

import pytest

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

        marked = _post(port, "/api/mark", {"label": "power on"})
        assert marked["ok"] is True and marked["marker"]["label"] == "power on"

        stopped = _post(port, "/api/stop", {})
        assert stopped["ok"] is True and stopped["stopped"] is True

        # stop again -> clear no-op, not an error.
        again_stop = _post(port, "/api/stop", {})
        assert again_stop["ok"] is True and again_stop["stopped"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()
