"""Tests for FfmpegRecorder.

No real ffmpeg and no real camera are used: ``subprocess.Popen`` and
``shutil.which`` are patched, and the argument builder is exercised directly.
"""

from __future__ import annotations

import collections
import signal
import subprocess
from unittest import mock

import pytest

from benchcam import session as session_mod
from benchcam.recorders import get_recorder
from benchcam.recorders.base import RecorderError
from benchcam.recorders.ffmpeg import (
    CAPTURE_FILENAME,
    CAPTURE_FILENAME_MKV,
    DEFAULT_AUDIO_DEVICE,
    DEFAULT_CAPTURE_RATE_MB_PER_MIN,
    DEFAULT_MIN_SESSION_MINUTES,
    DEFAULT_VIDEO_DEVICE,
    ENV_CAMERA,
    ENV_CAPTURE_RATE_MB_MIN,
    ENV_FRAMERATE,
    ENV_INPUT_FORMAT,
    ENV_MICROPHONE,
    ENV_MIN_SESSION_MINUTES,
    ENV_VIDEO_SIZE,
    PIDFILE_FILENAME,
    FfmpegRecorder,
    build_ffmpeg_command,
    build_list_devices_command,
    capture_filename,
    required_free_bytes,
    _resolve_float_knob,
)

DEVICE = "HD Pro Webcam C920S"
LINUX_DEVICE = "/dev/video0"
ALSA_DEVICE = "plughw:CARD=Nano,DEV=0"
# SIGKILL on POSIX; SIGTERM stand-in where SIGKILL is unavailable (Windows).
EXPECTED_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

# 64 GB — well above the default 12.2 GB pre-flight floor.
_AMPLE_FREE = 64_000_000_000


def _usage(free):
    """A shutil.disk_usage-shaped result with the given free bytes."""
    return collections.namedtuple("usage", ["total", "used", "free"])(
        total=_AMPLE_FREE, used=max(_AMPLE_FREE - free, 0), free=free
    )


@pytest.fixture(autouse=True)
def _ample_disk_space(monkeypatch):
    """Default every start() to a disk with ample room.

    start()'s pre-flight free-space guard calls shutil.disk_usage; unit tests
    must not depend on the real free space of the machine running them (least of
    all the Pi checkout, whose card is the very thing that fills). Tests that
    exercise the refusal path re-patch disk_usage to a low value themselves.
    """
    monkeypatch.setattr(
        "benchcam.recorders.ffmpeg.shutil.disk_usage",
        lambda _p: _usage(_AMPLE_FREE),
    )


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


def test_build_command_darwin_is_todo_stub(tmp_path):
    with pytest.raises(RecorderError) as exc:
        build_ffmpeg_command(
            DEVICE, tmp_path / CAPTURE_FILENAME, platform="darwin"
        )
    assert "avfoundation" in str(exc.value)


def test_build_command_linux_v4l2_with_audio(tmp_path):
    output = tmp_path / CAPTURE_FILENAME_MKV
    cmd = build_ffmpeg_command(
        LINUX_DEVICE,
        output,
        ffmpeg="ffmpeg",
        platform="linux",
        audio_device=ALSA_DEVICE,
    )

    assert _pair_at(cmd, "-f", "v4l2")
    assert _pair_at(cmd, "-input_format", "mjpeg")
    assert _pair_at(cmd, "-video_size", "1920x1080")
    assert _pair_at(cmd, "-framerate", "30")
    assert _pair_at(cmd, "-i", LINUX_DEVICE)
    # MJPEG must be stream-copied (the Pi must not transcode).
    assert _pair_at(cmd, "-c:v", "copy")
    assert "libx264" not in cmd
    # Audio: ALSA input, muxed into the capture file.
    assert _pair_at(cmd, "-f", "alsa")
    assert _pair_at(cmd, "-i", ALSA_DEVICE)
    assert "-an" not in cmd
    assert cmd[-1] == str(output)
    assert cmd[-1].endswith(".mkv")


def test_build_command_linux_without_audio_is_video_only(tmp_path):
    cmd = build_ffmpeg_command(
        LINUX_DEVICE, tmp_path / CAPTURE_FILENAME_MKV, platform="linux"
    )
    assert _pair_at(cmd, "-c:v", "copy")
    assert "-an" in cmd
    assert "alsa" not in cmd


def test_build_command_linux_respects_format_resolution_fps(tmp_path):
    cmd = build_ffmpeg_command(
        LINUX_DEVICE,
        tmp_path / CAPTURE_FILENAME_MKV,
        platform="linux",
        width=1280,
        height=720,
        fps=24,
        input_format="yuyv422",
    )
    assert _pair_at(cmd, "-video_size", "1280x720")
    assert _pair_at(cmd, "-framerate", "24")
    assert _pair_at(cmd, "-input_format", "yuyv422")


def test_capture_filename_is_platform_aware():
    assert capture_filename("linux") == CAPTURE_FILENAME_MKV
    assert capture_filename("win32") == CAPTURE_FILENAME
    assert capture_filename("darwin") == CAPTURE_FILENAME


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
    # Windows/dshow has no default device, so a blank camera must raise. (Linux
    # defaults to /dev/video0, covered separately.)
    monkeypatch.delenv(ENV_CAMERA, raising=False)
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "win32")
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


def test_start_linux_defaults_to_video0_yeti_and_mkv(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_CAMERA, raising=False)
    monkeypatch.delenv(ENV_MICROPHONE, raising=False)
    monkeypatch.delenv(ENV_VIDEO_SIZE, raising=False)
    monkeypatch.delenv(ENV_FRAMERATE, raising=False)
    monkeypatch.delenv(ENV_INPUT_FORMAT, raising=False)
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "linux")
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)  # blank camera/microphone

    with mock.patch(
        "benchcam.recorders.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch(
        "benchcam.recorders.ffmpeg.subprocess.Popen", return_value=mock.MagicMock()
    ) as popen:
        rec = FfmpegRecorder()
        rec.start(session.folder)

    command = popen.call_args.args[0]
    assert "v4l2" in command
    assert DEFAULT_VIDEO_DEVICE in command
    assert DEFAULT_AUDIO_DEVICE in command
    assert "copy" in command  # -c:v copy (no transcode)
    assert command[-1].endswith(CAPTURE_FILENAME_MKV)
    assert rec.output_path == session.folder / CAPTURE_FILENAME_MKV


def test_start_linux_blank_microphone_disables_audio(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_MICROPHONE, raising=False)
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "linux")
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root, camera="/dev/video2")

    with mock.patch(
        "benchcam.recorders.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch(
        "benchcam.recorders.ffmpeg.subprocess.Popen", return_value=mock.MagicMock()
    ) as popen:
        FfmpegRecorder(microphone="").start(session.folder)

    command = popen.call_args.args[0]
    assert "/dev/video2" in command
    assert "alsa" not in command
    assert "-an" in command


def test_start_linux_respects_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VIDEO_SIZE, "1280x720")
    monkeypatch.setenv(ENV_FRAMERATE, "24")
    monkeypatch.setenv(ENV_INPUT_FORMAT, "yuyv422")
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "linux")
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)

    with mock.patch(
        "benchcam.recorders.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch(
        "benchcam.recorders.ffmpeg.subprocess.Popen", return_value=mock.MagicMock()
    ) as popen:
        FfmpegRecorder().start(session.folder)

    command = popen.call_args.args[0]
    assert _pair_at(command, "-video_size", "1280x720")
    assert _pair_at(command, "-framerate", "24")
    assert _pair_at(command, "-input_format", "yuyv422")


def test_start_writes_pidfile_into_session_folder(tmp_path, monkeypatch):
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "linux")
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)

    fake_proc = mock.MagicMock()
    fake_proc.pid = 4321
    with mock.patch(
        "benchcam.recorders.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch(
        "benchcam.recorders.ffmpeg.subprocess.Popen", return_value=fake_proc
    ):
        FfmpegRecorder().start(session.folder)

    # A later, separate process must be able to find the PID to stop ffmpeg.
    assert (session.folder / PIDFILE_FILENAME).read_text().strip() == "4321"


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


def test_stop_linux_terminates_then_kills_when_graceful_times_out():
    # Linux/v4l2 path: SIGTERM is the primary stop, and if it doesn't exit in
    # time the SIGKILL fallback must ALWAYS run (and stop() must not raise).
    events = []
    proc = mock.MagicMock()
    proc.poll.return_value = None  # still running
    proc.terminate.side_effect = lambda: events.append("terminate")
    proc.kill.side_effect = lambda: events.append("kill")
    # First wait (after SIGTERM) times out; the post-kill wait reaps cleanly.
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="ffmpeg", timeout=8), 0]

    rec = FfmpegRecorder()
    rec._use_signal_stop = True  # as set by start() on Linux
    rec._process = proc
    rec.stop()  # must not raise

    assert events == ["terminate", "kill"]
    # The Linux path must not rely on 'q'-to-stdin.
    proc.stdin.write.assert_not_called()


def test_stop_linux_does_not_kill_when_sigterm_exits_cleanly():
    events = []
    proc = mock.MagicMock()
    proc.poll.return_value = None
    proc.terminate.side_effect = lambda: events.append("terminate")
    proc.kill.side_effect = lambda: events.append("kill")
    proc.wait.return_value = 0  # SIGTERM stops ffmpeg promptly

    rec = FfmpegRecorder()
    rec._use_signal_stop = True
    rec._process = proc
    rec.stop()

    assert events == ["terminate"]  # no SIGKILL needed
    proc.stdin.write.assert_not_called()


def test_stop_via_pidfile_signals_pid_from_a_different_instance(tmp_path, monkeypatch):
    # The real CLI flow: `benchcam run` starts ffmpeg in one process; a DIFFERENT
    # process runs `benchcam end`, so its recorder has no in-memory handle
    # (self._process is None) and must stop ffmpeg via the persisted pidfile.
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "linux")
    (tmp_path / PIDFILE_FILENAME).write_text("4321\n", encoding="utf-8")

    sent = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        # Alive until our SIGTERM lands, then liveness probes report it gone.
        if sig == 0 and (pid, signal.SIGTERM) in sent:
            raise ProcessLookupError

    monkeypatch.setattr("benchcam.recorders.ffmpeg.os.kill", fake_kill)

    rec = FfmpegRecorder()  # fresh instance — no Popen handle, like cmd_end
    assert rec._process is None
    rec.stop(tmp_path)  # must not raise

    assert (4321, signal.SIGTERM) in sent  # signaled the persisted PID
    # The pidfile is cleaned up once the process is gone.
    assert not (tmp_path / PIDFILE_FILENAME).exists()


def test_stop_via_pidfile_escalates_to_sigkill_when_sigterm_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "linux")
    # Zero timeout: the SIGTERM wait returns after a single liveness probe, so
    # the test escalates to SIGKILL immediately without spinning on the clock.
    monkeypatch.setattr("benchcam.recorders.ffmpeg.STOP_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr("benchcam.recorders.ffmpeg.time.sleep", lambda _s: None)
    (tmp_path / PIDFILE_FILENAME).write_text("4321\n", encoding="utf-8")

    sent = []

    def fake_kill(pid, sig):
        sent.append((pid, sig))
        # Stays alive through SIGTERM (sig 0 probes keep succeeding) until the
        # SIGKILL, after which liveness probes report it gone.
        if sig == 0 and (pid, EXPECTED_SIGKILL) in sent:
            raise ProcessLookupError

    monkeypatch.setattr("benchcam.recorders.ffmpeg.os.kill", fake_kill)

    FfmpegRecorder().stop(tmp_path)

    assert (4321, signal.SIGTERM) in sent
    assert (4321, EXPECTED_SIGKILL) in sent  # escalated to SIGKILL
    assert not (tmp_path / PIDFILE_FILENAME).exists()


def test_stop_via_pidfile_missing_file_is_noop(tmp_path):
    FfmpegRecorder().stop(tmp_path)  # no pidfile; must not raise


def test_stop_via_pidfile_stale_pid_is_cleaned_up(tmp_path, monkeypatch):
    (tmp_path / PIDFILE_FILENAME).write_text("999999\n", encoding="utf-8")
    monkeypatch.setattr(
        "benchcam.recorders.ffmpeg.os.kill",
        mock.Mock(side_effect=ProcessLookupError),  # process already gone
    )
    FfmpegRecorder().stop(tmp_path)  # must not raise
    assert not (tmp_path / PIDFILE_FILENAME).exists()


def test_stop_is_safe_when_never_started():
    rec = FfmpegRecorder()
    rec.stop()  # no process; must not raise


# --------------------------------------------------------------------------- #
# Pre-flight free-space guard
# --------------------------------------------------------------------------- #

def test_required_free_bytes_arithmetic():
    # 20 min x 610 MB/min x 1e6 = 12.2 GB.
    assert required_free_bytes(610, 20) == 12_200_000_000
    # Negative/zero knobs clamp to 0 (floor disabled), never a negative floor.
    assert required_free_bytes(-5, 20) == 0
    assert required_free_bytes(610, 0) == 0


def test_resolve_float_knob_precedence(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    # session.json carries a value; the env carries a different one.
    session = session_mod.create_session(root=root, min_session_minutes=15)
    monkeypatch.setenv(ENV_MIN_SESSION_MINUTES, "8")

    args = ("min_session_minutes", ENV_MIN_SESSION_MINUTES, DEFAULT_MIN_SESSION_MINUTES)

    # constructor override wins over everything.
    assert _resolve_float_knob(30.0, session.folder, *args) == 30.0
    # no override -> session.json wins over env.
    assert _resolve_float_knob(None, session.folder, *args) == 15.0

    # no override, no session value -> env wins over default.
    bare = session_mod.create_session(root=root)  # no knob written
    assert _resolve_float_knob(None, bare.folder, *args) == 8.0
    # nothing set anywhere -> default.
    monkeypatch.delenv(ENV_MIN_SESSION_MINUTES, raising=False)
    assert _resolve_float_knob(None, bare.folder, *args) == DEFAULT_MIN_SESSION_MINUTES


def test_start_refuses_below_free_space_floor_and_never_spawns(tmp_path, monkeypatch):
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "linux")
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root, camera="/dev/video0")
    # Only 1 GB free — far below the 12.2 GB default floor.
    monkeypatch.setattr(
        "benchcam.recorders.ffmpeg.shutil.disk_usage",
        lambda _p: _usage(1_000_000_000),
    )

    with mock.patch(
        "benchcam.recorders.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch("benchcam.recorders.ffmpeg.subprocess.Popen") as popen:
        rec = FfmpegRecorder()
        with pytest.raises(RecorderError) as exc:
            rec.start(session.folder)

    msg = str(exc.value)
    assert "free space" in msg.lower()
    assert "NOT started" in msg  # tells the operator ffmpeg did not run
    popen.assert_not_called()  # ffmpeg must never spawn below the floor
    # Nothing half-started left behind on the recorder or on disk.
    assert rec.output_path is None
    assert not (session.folder / PIDFILE_FILENAME).exists()


def test_start_allows_when_free_space_exactly_meets_floor(tmp_path, monkeypatch):
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "linux")
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root, camera="/dev/video0")
    # Exactly the default floor -> allowed (the check is free >= required).
    floor = required_free_bytes(
        DEFAULT_CAPTURE_RATE_MB_PER_MIN, DEFAULT_MIN_SESSION_MINUTES
    )
    monkeypatch.setattr(
        "benchcam.recorders.ffmpeg.shutil.disk_usage", lambda _p: _usage(floor)
    )

    with mock.patch(
        "benchcam.recorders.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch(
        "benchcam.recorders.ffmpeg.subprocess.Popen", return_value=mock.MagicMock()
    ) as popen:
        FfmpegRecorder().start(session.folder)

    popen.assert_called_once()  # at the floor, capture proceeds


def test_start_reads_free_space_at_start_not_creation(tmp_path, monkeypatch):
    # A card that had room at 'new' but filled before 'run' must still be caught:
    # the guard reads disk_usage at start() time, not a value cached at creation.
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "linux")
    root = tmp_path / "sessions"
    # create_session runs under the autouse ample-disk fixture (room at 'new').
    session = session_mod.create_session(root=root, camera="/dev/video0")
    # Now the card is nearly full at run time.
    monkeypatch.setattr(
        "benchcam.recorders.ffmpeg.shutil.disk_usage",
        lambda _p: _usage(500_000_000),
    )

    with mock.patch(
        "benchcam.recorders.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch("benchcam.recorders.ffmpeg.subprocess.Popen") as popen:
        with pytest.raises(RecorderError):
            FfmpegRecorder().start(session.folder)

    popen.assert_not_called()


def test_start_floor_honors_session_json_override(tmp_path, monkeypatch):
    # A per-session floor from session.json raises the bar: 40 min x 610 MB/min
    # ~= 24.4 GB, so 20 GB free (fine under the default) is now refused.
    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "linux")
    root = tmp_path / "sessions"
    session = session_mod.create_session(
        root=root, camera="/dev/video0", min_session_minutes=40
    )
    monkeypatch.setattr(
        "benchcam.recorders.ffmpeg.shutil.disk_usage",
        lambda _p: _usage(20_000_000_000),
    )

    with mock.patch(
        "benchcam.recorders.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch("benchcam.recorders.ffmpeg.subprocess.Popen") as popen:
        with pytest.raises(RecorderError):
            FfmpegRecorder().start(session.folder)

    popen.assert_not_called()


def test_run_below_floor_never_reaches_start_session(tmp_path, monkeypatch):
    # End-to-end through the CLI 'run' path: below the floor, the refusal must
    # surface as a clean error AND leave no half-running session — start_session()
    # is never reached, so status stays 'created' with no start stamp. (The
    # .active pointer already exists from 'new'; the meaningful check is that the
    # session was never marked running.)
    from benchcam import cli

    monkeypatch.setattr("benchcam.recorders.ffmpeg.sys.platform", "linux")
    root = tmp_path / "sessions"
    session = session_mod.create_session(
        root=root, camera="/dev/video0", recorder="ffmpeg"
    )
    monkeypatch.setattr(
        "benchcam.recorders.ffmpeg.shutil.disk_usage",
        lambda _p: _usage(1_000_000_000),
    )

    started = []
    monkeypatch.setattr(session_mod, "start_session", lambda s: started.append(s))

    with mock.patch(
        "benchcam.recorders.ffmpeg.shutil.which", return_value="/usr/bin/ffmpeg"
    ), mock.patch("benchcam.recorders.ffmpeg.subprocess.Popen") as popen:
        rc = cli.main(["run", "--sessions-root", str(root)])

    assert rc == 1  # refusal surfaced as a clean non-zero exit
    popen.assert_not_called()  # ffmpeg never spawned
    assert started == []  # start_session() never reached
    reloaded = session_mod.load_session(session.folder)
    assert reloaded.status == session_mod.STATUS_CREATED
    assert reloaded.started_wall_time is None


def test_get_recorder_ffmpeg_returns_instance():
    assert isinstance(get_recorder("ffmpeg"), FfmpegRecorder)
