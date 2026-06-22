"""FfmpegRecorder: capture video (and, on Linux, audio) via an external ``ffmpeg``.

This drives ``ffmpeg`` as a subprocess (no Python ffmpeg dependency — runtime
stays stdlib only). It writes a single capture file into the session folder,
started at the same moment the session starts so its timecode lines up with
marker ``elapsed_seconds`` (which is measured from session start).

Two capture paths, selected by platform:

Windows / DirectShow (``capture.mp4``):
    Video only, transcoded to H.264. We stop ffmpeg gracefully by sending ``q``
    to its stdin, which lets it write the MP4 ``moov`` atom and finalize a
    playable file. MP4 is the most portable output for playback. The only lossy
    case is the force-kill fallback (ffmpeg ignored ``q`` past the timeout).

Linux / V4L2 + ALSA (``capture.mkv``) — e.g. a Raspberry Pi 5 capturing from a
Logitech C920:
    The C920 emits MJPEG natively, so we request the MJPEG stream and
    stream-copy it (``-c:v copy``) — the Pi must NOT transcode. Audio (e.g. a
    Yeti Nano) is read from ALSA and muxed alongside the video. The container is
    Matroska (``.mkv``) because MJPEG stream-copy is unreliable in ``.mp4``.
    Harmless ``Dequeued v4l2 buffer contains corrupted data`` warnings are
    expected at stream startup; ffmpeg logs them to ``ffmpeg.log`` and the
    recorder never parses stderr, so they are not treated as fatal.

Device strings, capture format, resolution and frame rate are configurable (no
hardcoded ``/dev/video0`` or Yeti string in the command builder). The video
device resolves (in priority order) from: an explicit constructor argument, the
session's ``camera`` field in ``session.json`` (``benchcam new --camera``), the
``BENCHCAM_CAMERA`` environment variable, and finally a per-platform default
(``/dev/video0`` on Linux; Windows has no default and requires an explicit
device name). The ALSA audio device resolves the same way from the constructor /
``microphone`` field / ``BENCHCAM_MICROPHONE`` / the Yeti default. Resolution,
frame rate and the V4L2 input format can be overridden with ``BENCHCAM_VIDEO_SIZE``
(``WxH``), ``BENCHCAM_FRAMERATE`` and ``BENCHCAM_INPUT_FORMAT``.

macOS (avfoundation) is left as a clearly-marked TODO stub.

Capture only — never commands moving hardware.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .base import Recorder, RecorderError

#: Windows/dshow output (transcoded H.264 — finalizes a portable MP4).
CAPTURE_FILENAME = "capture.mp4"
#: Linux/v4l2 output (MJPEG stream-copy is unreliable in .mp4, so use Matroska).
CAPTURE_FILENAME_MKV = "capture.mkv"
FFMPEG_LOG_FILENAME = "ffmpeg.log"
SESSION_FILENAME = "session.json"
#: Records the running ffmpeg PID so a later, separate process (``benchcam end``)
#: can stop the capture that ``benchcam run`` started — they are different
#: processes, so the in-memory Popen handle is not shared between them.
PIDFILE_FILENAME = "ffmpeg.pid"

#: SIGKILL is POSIX-only; on Windows os.kill treats any non-CTRL signal as a
#: hard TerminateProcess, so SIGTERM is a safe stand-in there.
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

ENV_CAMERA = "BENCHCAM_CAMERA"
ENV_MICROPHONE = "BENCHCAM_MICROPHONE"
ENV_VIDEO_SIZE = "BENCHCAM_VIDEO_SIZE"
ENV_FRAMERATE = "BENCHCAM_FRAMERATE"
ENV_INPUT_FORMAT = "BENCHCAM_INPUT_FORMAT"

DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_FPS = 30
#: The C920 emits this natively; stream-copied as-is on Linux.
DEFAULT_INPUT_FORMAT = "mjpeg"
#: Default V4L2 capture device on Linux (override via --camera / BENCHCAM_CAMERA).
DEFAULT_VIDEO_DEVICE = "/dev/video0"
#: Default ALSA capture device (Yeti Nano); override via --microphone / env.
DEFAULT_AUDIO_DEVICE = "plughw:CARD=Nano,DEV=0"
DEFAULT_AUDIO_RATE = 44100
DEFAULT_AUDIO_CHANNELS = 2

#: Seconds to wait for ffmpeg to finalize after a graceful stop before force-kill.
STOP_TIMEOUT_SECONDS = 8.0

_INSTALL_HINT = (
    "ffmpeg was not found on PATH. Install it and try again. "
    "Windows: 'winget install Gyan.FFmpeg' (or download from "
    "https://ffmpeg.org/download.html and add it to PATH). "
    "Linux: 'sudo apt install ffmpeg'. macOS: 'brew install ffmpeg'."
)

_NO_CAMERA_HINT = (
    "No camera device configured for the ffmpeg recorder. Provide one with "
    "'benchcam new --camera \"<device>\"' (stored in session.json) or set the "
    "BENCHCAM_CAMERA environment variable. On Windows the dshow name is found "
    "with: ffmpeg -list_devices true -f dshow -i dummy. On Linux it is a V4L2 "
    "path like /dev/video0 (discover with: v4l2-ctl --list-devices)."
)


def build_list_devices_command(ffmpeg: str = "ffmpeg") -> list[str]:
    """Return the ffmpeg command that lists DirectShow capture devices.

    Run it and read its stderr to find the exact camera/microphone names to
    pass as ``--camera``.
    """
    return [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]


def build_ffmpeg_command(
    device_name: str,
    output_path: Path | str,
    *,
    ffmpeg: str = "ffmpeg",
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    fps: int = DEFAULT_FPS,
    platform: str | None = None,
    audio_device: str | None = None,
    input_format: str = DEFAULT_INPUT_FORMAT,
    audio_rate: int = DEFAULT_AUDIO_RATE,
    audio_channels: int = DEFAULT_AUDIO_CHANNELS,
) -> list[str]:
    """Build the ffmpeg argument list for a single-camera capture.

    Pure and side-effect free so it can be unit-tested without spawning ffmpeg.
    ``platform`` defaults to :data:`sys.platform` but can be passed explicitly in
    tests. Windows uses DirectShow (video only, transcoded). Linux uses V4L2 with
    ``-c:v copy`` (no transcode) and, when ``audio_device`` is set, muxes ALSA
    audio. macOS is a TODO stub.
    """
    if platform is None:
        platform = sys.platform
    if not device_name:
        raise RecorderError(_NO_CAMERA_HINT)

    output = str(output_path)

    if platform.startswith("win"):
        input_args = [
            "-f", "dshow",
            "-video_size", f"{width}x{height}",
            "-framerate", str(fps),
            # The C920S exposes 1080p30 over MJPEG (raw YUY2 can't hit 30fps at
            # 1080p), so request the MJPEG stream explicitly.
            "-vcodec", "mjpeg",
            "-rtbufsize", "100M",
            "-i", f"video={device_name}",
        ]
        encoder_args = [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-an",  # video only on the Windows/dshow path
            "-movflags", "+faststart",
        ]
        return [ffmpeg, "-hide_banner", "-y", *input_args, *encoder_args, output]

    if platform.startswith("linux"):
        # V4L2: the C920 emits MJPEG natively, so request that stream and
        # stream-copy it (-c:v copy). The Pi must not transcode.
        input_args = [
            "-f", "v4l2",
            "-input_format", input_format,
            "-framerate", str(fps),
            "-video_size", f"{width}x{height}",
            "-i", device_name,
        ]
        encoder_args = ["-c:v", "copy"]
        if audio_device:
            # ALSA audio (e.g. the Yeti Nano), muxed alongside the video.
            input_args += [
                "-f", "alsa",
                "-ac", str(audio_channels),
                "-ar", str(audio_rate),
                "-i", audio_device,
            ]
            encoder_args += ["-c:a", "aac"]
        else:
            encoder_args += ["-an"]
        # Matroska (.mkv): MJPEG stream-copy is unreliable in .mp4.
        return [ffmpeg, "-hide_banner", "-y", *input_args, *encoder_args, output]

    if platform == "darwin":
        # TODO (POSIX/macOS, avfoundation): real implementation would be roughly
        #   ["-f", "avfoundation", "-framerate", str(fps),
        #    "-video_size", f"{width}x{height}", "-i", device_name]
        # where device_name is an avfoundation index/name (discover with
        # `ffmpeg -f avfoundation -list_devices true -i ""`).
        raise RecorderError(_posix_unsupported("macOS", "avfoundation"))

    raise RecorderError(
        f"Unsupported platform {platform!r} for ffmpeg capture; "
        "Windows (dshow) and Linux (v4l2) are the supported targets."
    )


def _posix_unsupported(os_label: str, api: str) -> str:
    return (
        f"FfmpegRecorder on {os_label} is not implemented yet (the {api} input "
        "path is a TODO). Windows (dshow) and Linux (v4l2) are the supported "
        "targets; use the 'null' recorder elsewhere."
    )


def capture_filename(platform: str | None = None) -> str:
    """Capture file name for ``platform`` (Matroska on Linux, MP4 elsewhere)."""
    if platform is None:
        platform = sys.platform
    return CAPTURE_FILENAME_MKV if platform.startswith("linux") else CAPTURE_FILENAME


def _field_from_session(storage_path: Path, key: str) -> str:
    """Read a string field (e.g. ``camera``) from ``session.json``."""
    session_file = Path(storage_path) / SESSION_FILENAME
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _resolve_video_size() -> tuple[int, int]:
    """Resolve capture resolution from ``BENCHCAM_VIDEO_SIZE`` (``WxH``) or default."""
    raw = os.environ.get(ENV_VIDEO_SIZE, "").strip().lower()
    match = re.fullmatch(r"(\d+)x(\d+)", raw)
    if match:
        return int(match.group(1)), int(match.group(2))
    return DEFAULT_WIDTH, DEFAULT_HEIGHT


def _resolve_fps() -> int:
    """Resolve frame rate from ``BENCHCAM_FRAMERATE`` or default."""
    raw = os.environ.get(ENV_FRAMERATE, "").strip()
    try:
        return int(raw) if raw else DEFAULT_FPS
    except ValueError:
        return DEFAULT_FPS


class FfmpegRecorder(Recorder):
    """Record one webcam capture per session via an ffmpeg subprocess."""

    name = "ffmpeg"

    def __init__(self, camera: str | None = None, microphone: str | None = None) -> None:
        self._camera = camera
        # ``None`` means "fall back to session.json/env/default"; an explicit ""
        # disables audio (video only) on the Linux path.
        self._microphone = microphone
        self._process: subprocess.Popen | None = None
        self._log_handle = None
        self._output_path: Path | None = None
        self._storage_path: Path | None = None
        # Linux/v4l2 capture does not stop reliably via 'q' on stdin, so stop()
        # uses SIGTERM->SIGKILL there. Set in start(); defaults to the Windows
        # 'q'-to-stdin path so a directly-driven recorder keeps that behavior.
        self._use_signal_stop = False

    @property
    def output_path(self) -> Path | None:
        return self._output_path

    def _resolve_device(self, storage_path: Path) -> str:
        """Resolve the video device (constructor > session.json > env > default)."""
        if self._camera:
            return self._camera.strip()
        from_session = _field_from_session(storage_path, "camera")
        if from_session:
            return from_session
        from_env = os.environ.get(ENV_CAMERA, "").strip()
        if from_env:
            return from_env
        if sys.platform.startswith("linux"):
            return DEFAULT_VIDEO_DEVICE
        return ""

    def _resolve_audio(self, storage_path: Path) -> str:
        """Resolve the ALSA audio device on Linux (constructor > session > env > default).

        Returns "" on non-Linux platforms (the Windows/dshow path is video-only)
        and when audio is explicitly disabled via ``microphone=""``.
        """
        if not sys.platform.startswith("linux"):
            return ""
        if self._microphone is not None:
            return self._microphone.strip()
        from_session = _field_from_session(storage_path, "microphone")
        if from_session:
            return from_session
        from_env = os.environ.get(ENV_MICROPHONE, "").strip()
        if from_env:
            return from_env
        return DEFAULT_AUDIO_DEVICE

    def start(self, storage_path: Path) -> None:
        storage_path = Path(storage_path)

        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin is None:
            raise RecorderError(_INSTALL_HINT)

        device = self._resolve_device(storage_path)
        if not device:
            raise RecorderError(_NO_CAMERA_HINT)
        audio_device = self._resolve_audio(storage_path)
        width, height = _resolve_video_size()
        fps = _resolve_fps()
        input_format = (
            os.environ.get(ENV_INPUT_FORMAT, "").strip() or DEFAULT_INPUT_FORMAT
        )

        output_path = storage_path / capture_filename()
        command = build_ffmpeg_command(
            device,
            output_path,
            ffmpeg=ffmpeg_bin,
            width=width,
            height=height,
            fps=fps,
            audio_device=audio_device,
            input_format=input_format,
        )

        storage_path.mkdir(parents=True, exist_ok=True)
        # ffmpeg's progress/errors go to stderr; keep a log inside the session
        # folder (which is gitignored) so failed captures can be diagnosed. On
        # Linux, expect harmless "Dequeued v4l2 buffer contains corrupted data"
        # warnings here at startup — they are not fatal.
        self._log_handle = (storage_path / FFMPEG_LOG_FILENAME).open("wb")
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._log_handle,
            )
        except OSError as exc:  # pragma: no cover - defensive
            self._close_log()
            raise RecorderError(f"Failed to launch ffmpeg: {exc}") from exc
        # The Linux/v4l2 path needs SIGTERM->SIGKILL on stop (q-to-stdin is
        # unreliable for V4L2); the Windows/dshow path keeps the 'q' finalize.
        self._use_signal_stop = sys.platform.startswith("linux")
        self._output_path = output_path
        self._storage_path = storage_path
        # Persist the PID so a *different* process (benchcam end vs. benchcam run)
        # can find and stop this ffmpeg — otherwise it runs orphaned and fills
        # the disk. Best-effort: a failed write must not abort the capture.
        try:
            (storage_path / PIDFILE_FILENAME).write_text(
                f"{self._process.pid}\n", encoding="utf-8"
            )
        except OSError:  # pragma: no cover - defensive
            pass

    def stop(self, storage_path: Path | None = None) -> None:
        """Stop ffmpeg, guaranteeing the process is dead before returning.

        Two cases:

        * In-process (the dashboard / ``benchcam live``): we still hold the
          ``Popen`` handle, so stop it directly.
        * Cross-process (``benchcam end`` after ``benchcam run``): the handle is
          ``None`` because a different process started ffmpeg. We read the PID
          from ``ffmpeg.pid`` in the session folder and stop *that* PID instead,
          so the capture can never run orphaned and fill the disk.

        A runaway ffmpeg on a headless Pi fills the SD card, so the SIGKILL
        fallback ALWAYS runs if the primary stop does not exit in time, and we
        block until the process is confirmed gone before returning.
        """
        folder = Path(storage_path) if storage_path is not None else self._storage_path
        proc = self._process

        if proc is not None:
            # In-process: use the live handle.
            try:
                self._stop_handle(proc)
            finally:
                self._process = None
                self._close_log()
            self._remove_pidfile(folder)
            return

        # Cross-process: no handle, so fall back to the persisted PID.
        self._close_log()
        if folder is not None:
            self._stop_via_pidfile(folder)

    def _stop_handle(self, proc: subprocess.Popen) -> None:
        """Stop ffmpeg via the in-memory Popen handle (SIGTERM/q -> SIGKILL)."""
        if proc.poll() is not None:
            return
        if self._use_signal_stop:
            # Linux/v4l2: 'q' on stdin does not reliably stop a V4L2 capture,
            # so SIGTERM is the primary stop.
            proc.terminate()
        else:
            # Windows/dshow: 'q' lets ffmpeg write the mp4 moov atom and
            # finalize a playable file.
            self._graceful_stop(proc)
        if not self._wait_for_exit(proc, STOP_TIMEOUT_SECONDS):
            # Primary stop didn't exit in time; SIGKILL, then block until the
            # kill is reaped so we never leave ffmpeg writing.
            proc.kill()
            self._wait_for_exit(proc, None)

    def _stop_via_pidfile(self, folder: Path) -> None:
        """Read ``ffmpeg.pid`` and stop that PID; tolerate a stale/missing file.

        Mirrors the in-memory SIGTERM->SIGKILL logic but on a bare PID, since the
        process that holds the Popen handle has already exited.
        """
        pidfile = Path(folder) / PIDFILE_FILENAME
        pid = self._read_pid(pidfile)
        if pid is not None:
            self._signal_pid_until_dead(pid)
        # Clean up the pidfile whether we signaled, found it stale, or it was
        # garbage — a leftover pidfile must never mislead a future stop.
        self._remove_pidfile(folder)

    @staticmethod
    def _read_pid(pidfile: Path) -> int | None:
        """Parse a positive PID from ``pidfile``, or None if missing/garbage."""
        try:
            text = pidfile.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            pid = int(text)
        except ValueError:
            return None
        return pid if pid > 0 else None

    def _signal_pid_until_dead(self, pid: int) -> None:
        """SIGTERM, wait up to the timeout, then SIGKILL; block until gone.

        PID-reuse is an accepted risk on this single-user bench appliance, so we
        signal the PID directly without extra identity checks.
        """
        if not self._pid_alive(pid):
            return
        self._send_signal(pid, signal.SIGTERM)
        if self._wait_pid_gone(pid, STOP_TIMEOUT_SECONDS):
            return
        # Did not exit in time — force kill and block until it is gone.
        self._send_signal(pid, _SIGKILL)
        self._wait_pid_gone(pid, STOP_TIMEOUT_SECONDS)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """True if ``pid`` exists. Signal 0 only probes; it does not kill."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # exists but owned by another user
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _send_signal(pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except OSError:
            pass  # already gone (or cannot signal) — liveness checks decide

    @classmethod
    def _wait_pid_gone(cls, pid: int, timeout: float) -> bool:
        """Poll until ``pid`` is gone or ``timeout`` elapses; True if gone."""
        deadline = time.monotonic() + timeout
        while True:
            if not cls._pid_alive(pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    @staticmethod
    def _remove_pidfile(folder: Path | None) -> None:
        if folder is None:
            return
        try:
            (Path(folder) / PIDFILE_FILENAME).unlink()
        except OSError:
            pass

    @staticmethod
    def _wait_for_exit(proc: subprocess.Popen, timeout: float | None) -> bool:
        """Wait for ``proc`` to exit; return True if it did, False on timeout.

        Swallows :class:`subprocess.TimeoutExpired` so callers can decide on the
        fallback — a propagating timeout must never skip the SIGKILL fallback.
        ``timeout=None`` blocks until the process is reaped.
        """
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def _graceful_stop(self, proc: subprocess.Popen) -> None:
        """Ask ffmpeg to finalize the file by sending 'q' to its stdin (dshow)."""
        stdin = proc.stdin
        if stdin is None:
            # No stdin to talk to; fall back to a polite terminate (SIGTERM /
            # CTRL on Windows) rather than an immediate kill.
            proc.terminate()
            return
        try:
            stdin.write(b"q")
            stdin.flush()
            stdin.close()
        except (OSError, ValueError):
            proc.terminate()

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            finally:
                self._log_handle = None
