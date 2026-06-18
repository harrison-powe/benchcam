"""Tests for FfmpegRecorder.

No real ffmpeg and no real camera are used: ``subprocess.Popen`` and
``shutil.which`` are patched, and the argument builder is exercised directly.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from benchcam import session as session_mod
from benchcam.recorders import get_recorder
from benchcam.recorders.base import RecorderError
from benchcam.recorders.ffmpeg import (
    CAPTURE_FILENAME,
    ENV_CAMERA,
    FfmpegRecorder,
    build_ffmpeg_command,
    build_list_devices_command,
)

DEVICE = "HD Pro Webcam C920S"


def _pair_at(cmd, flag, value):
    """True if ``flag`` is immediately followed by ``value`` in ``cmd``."""
    return any(
        cmd[i] == flag and cmd[i + 1] == value for i in range(len(cmd) - 1)
    )


# --------------------------------------------------------------------------- #
# Argument builder
# --------------------------------------------------------------------------- #

def test_build_command_windows_dshow(tmp_path):
    output = tmp_path / CAPTURE_FILENAME
    cmd = build_ffmpeg_command(DEVICE, output, ffmpeg="ffmpeg", platform="win32")

    assert cmd[0] == "ffmpeg"
    assert _pair_at(cmd, "-f", "dshow")
    assert _pair_at(cmd, "-i", f"video={DEVICE}")
    assert _pair_at(cmd, "-video_size", "1920x1080")
    assert _pair_at(cmd, "-framerate", "30")
    assert _pair_at(cmd, "-c:v", "libx264")
    assert cmd[-1] == str(output)
    assert cmd[-1].endswith(CAPTURE_FILENAME)


def test_build_command_respects_resolution_fps_and_binary(tmp_path):
    output = tmp_path / CAPTURE_FILENAME
    cmd = build_ffmpeg_command(
        DEVICE,
        output,
        ffmpeg="/opt/ffmpeg",
        width=1280,
        height=720,
        fps=24,
        platform="win32",
    )
    assert cmd[0] == "/opt/ffmpeg"
    assert _pair_at(cmd, "-video_size", "1280x720")
    assert _pair_at(cmd, "-framerate", "24")


def test_build_command_empty_device_raises(tmp_path):
    with pytest.raises(RecorderError):
        build_ffmpeg_command("", tmp_path / CAPTURE_FILENAME, platform="win32")


@pytest.mark.parametrize(
    "platform, api",
    [("linux", "v4l2"), ("darwin", "avfoundation")],
)
def test_build_command_posix_is_todo_stub(tmp_path, platform, api):
    with pytest.raises(RecorderError) as exc:
        build_ffmpeg_command(DEVICE, tmp_path / CAPTURE_FILENAME, platform=platform)
    assert api in str(exc.value)


def test_list_devices_command():
    cmd = build_list_devices_command("ffmpeg")
    assert _pair_at(cmd, "-f", "dshow")
    assert _pair_at(cmd, "-list_devices", "true")
    assert cmd[0] == "ffmpeg"


# --------------------------------------------------------------------------- #
# start()
# --------------------------------------------------------------------------- #

def test_start_raises_clearly_when_ffmpeg_missing(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_CAMERA, raising=False)
    with mock.patch("benchcam.recorders.ffmpeg.shutil.which", return_value=None):
        rec = FfmpegRecorder(camera=DEVICE)
        with pytest.raises(RecorderError) as exc:
            rec.start(tmp_path)
    msg = str(exc.value).lower()
    assert "ffmpeg" in msg and "path" in msg
    assert "install" in msg


def test_start_raises_when_no_camera_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_CAMERA, raising=False)
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)  # camera defaults to ""
    with mock.patch("benchcam.recorders.ffmpeg.shutil.which", return_value="/x/ffmpeg"):
        rec = FfmpegRecorder()
        with pytest.raises(RecorderError) as exc:
            rec.start(session.folder)
    assert "camera" in str(exc.value).lower()


def test_start_uses_camera_from_session_json_and_spawns(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_CAMERA, raising=False)
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "win32")
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root, camera=DEVICE)

    fake_proc = mock.MagicMock()
    with mock.patch(
        "benchcam.recorders.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch(
        "benchcam.recorders.ffmpeg.subprocess.Popen", return_value=fake_proc
    ) as popen:
        rec = FfmpegRecorder()
        rec.start(session.folder)

    command = popen.call_args.args[0]
    assert command[0] == "/usr/bin/ffmpeg"
    assert f"video={DEVICE}" in command
    assert command[-1].endswith(CAPTURE_FILENAME)
    assert str(session.folder) in command[-1]
    # ffmpeg needs stdin so stop() can send 'q'.
    assert popen.call_args.kwargs["stdin"] == subprocess.PIPE
    assert rec.output_path == session.folder / CAPTURE_FILENAME


def test_constructor_camera_overrides_session_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_CAMERA, "env-cam")
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "win32")
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root, camera="session-cam")

    with mock.patch(
        "benchcam.recorders.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch(
        "benchcam.recorders.ffmpeg.subprocess.Popen", return_value=mock.MagicMock()
    ) as popen:
        FfmpegRecorder(camera="ctor-cam").start(session.folder)

    assert "video=ctor-cam" in popen.call_args.args[0]


def test_env_camera_used_when_session_blank(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_CAMERA, "env-cam")
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "win32")
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)  # blank camera

    with mock.patch(
        "benchcam.recorders.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch(
        "benchcam.recorders.ffmpeg.subprocess.Popen", return_value=mock.MagicMock()
    ) as popen:
        FfmpegRecorder().start(session.folder)

    assert "video=env-cam" in popen.call_args.args[0]


# --------------------------------------------------------------------------- #
# stop()
# --------------------------------------------------------------------------- #

def _running_proc(events):
    proc = mock.MagicMock()
    proc.poll.return_value = None  # still running
    proc.stdin.write.side_effect = lambda data: events.append(("write", data))
    proc.kill.side_effect = lambda: events.append(("kill", None))
    return proc


def test_stop_sends_graceful_q_and_does_not_kill_on_clean_exit():
    events = []
    proc = _running_proc(events)
    proc.wait.return_value = 0  # finalizes promptly

    rec = FfmpegRecorder()
    rec._process = proc
    rec.stop()

    assert ("write", b"q") in events
    assert not any(kind == "kill" for kind, _ in events)
    proc.stdin.close.assert_called_once()


def test_stop_timeout_triggers_kill_fallback_after_graceful():
    events = []
    proc = _running_proc(events)
    proc.wait.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=8)

    rec = FfmpegRecorder()
    rec._process = proc
    rec.stop()

    kinds = [kind for kind, _ in events]
    assert "write" in kinds and "kill" in kinds
    assert kinds.index("write") < kinds.index("kill")  # graceful before kill


def test_stop_is_safe_when_never_started():
    rec = FfmpegRecorder()
    rec.stop()  # no process; must not raise


def test_get_recorder_ffmpeg_returns_instance():
    assert isinstance(get_recorder("ffmpeg"), FfmpegRecorder)
