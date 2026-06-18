"""FfmpegRecorder: capture video from a webcam via an external ``ffmpeg`` binary.

This drives ``ffmpeg`` as a subprocess (no Python ffmpeg dependency — runtime
stays stdlib only). It writes a single video file, ``capture.mp4``, into the
session folder, started at the same moment the session starts so its timecode
lines up with marker ``elapsed_seconds`` (which is measured from session start).

Container choice — ``capture.mp4``:
    We stop ffmpeg gracefully by sending ``q`` to its stdin, which lets it write
    the MP4 ``moov`` atom and finalize a playable file. MP4 is the most portable
    output for playback, so it is the default. The only lossy case is the
    force-kill fallback (ffmpeg ignored ``q`` past the timeout); a hard-killed
    MP4 may be truncated. If you expect frequent abrupt termination, MKV would be
    more resilient, but with graceful shutdown the common path produces a clean
    MP4 — and the user-facing flow asks to confirm ``capture.mp4`` plays.

Device name:
    The dshow device name is not knowable in advance, so it is resolved (in
    priority order) from: an explicit constructor argument, the session's
    ``camera`` field in ``session.json`` (set via ``benchcam new --camera``), and
    finally the ``BENCHCAM_CAMERA`` environment variable. List device names with
    :func:`build_list_devices_command` (or the command documented in the README).

Scope: video only, Windows/dshow is the supported target. POSIX (v4l2 /
avfoundation) is left as clearly-marked TODO stubs in :func:`build_ffmpeg_command`.
Capture only — never commands moving hardware.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .base import Recorder, RecorderError

CAPTURE_FILENAME = "capture.mp4"
FFMPEG_LOG_FILENAME = "ffmpeg.log"
SESSION_FILENAME = "session.json"
ENV_CAMERA = "BENCHCAM_CAMERA"

DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_FPS = 30

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
    "'benchcam new --camera \"<device name>\"' (stored in session.json) or set "
    "the BENCHCAM_CAMERA environment variable. Find the exact name on Windows "
    "with: ffmpeg -list_devices true -f dshow -i dummy"
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
) -> list[str]:
    """Build the ffmpeg argument list for a single-camera video capture.

    Pure and side-effect free so it can be unit-tested without spawning ffmpeg.
    ``platform`` defaults to :data:`sys.platform` but can be passed explicitly in
    tests. Windows (dshow) is the supported target; POSIX paths are TODO stubs.
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
    elif platform.startswith("linux"):
        # TODO (POSIX/Linux, v4l2): real implementation would be roughly
        #   ["-f", "v4l2", "-framerate", str(fps),
        #    "-video_size", f"{width}x{height}", "-i", device_name]
        # where device_name is e.g. "/dev/video0" (discover with
        # `v4l2-ctl --list-devices`). Windows is the supported target for now.
        raise RecorderError(_posix_unsupported("Linux", "v4l2"))
    elif platform == "darwin":
        # TODO (POSIX/macOS, avfoundation): real implementation would be roughly
        #   ["-f", "avfoundation", "-framerate", str(fps),
        #    "-video_size", f"{width}x{height}", "-i", device_name]
        # where device_name is an avfoundation index/name (discover with
        # `ffmpeg -f avfoundation -list_devices true -i ""`).
        raise RecorderError(_posix_unsupported("macOS", "avfoundation"))
    else:
        raise RecorderError(
            f"Unsupported platform {platform!r} for ffmpeg capture; "
            "Windows (dshow) is the supported target."
        )

    encoder_args = [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-an",  # video only for v0; no audio mixing
        "-movflags", "+faststart",
    ]

    return [ffmpeg, "-hide_banner", "-y", *input_args, *encoder_args, output]


def _posix_unsupported(os_label: str, api: str) -> str:
    return (
        f"FfmpegRecorder on {os_label} is not implemented yet (the {api} input "
        "path is a TODO). Windows (dshow) is the supported target for v0; use the "
        "'null' recorder elsewhere."
    )


def _camera_from_session(storage_path: Path) -> str:
    """Read the ``camera`` field from ``session.json`` in the session folder."""
    session_file = Path(storage_path) / SESSION_FILENAME
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get("camera", "")
    return value.strip() if isinstance(value, str) else ""


class FfmpegRecorder(Recorder):
    """Record one webcam video per session via an ffmpeg subprocess."""

    name = "ffmpeg"

    def __init__(self, camera: str | None = None) -> None:
        self._camera = camera
        self._process: subprocess.Popen | None = None
        self._log_handle = None
        self._output_path: Path | None = None

    @property
    def output_path(self) -> Path | None:
        return self._output_path

    def _resolve_device(self, storage_path: Path) -> str:
        """Resolve the dshow device name (constructor > session.json > env)."""
        if self._camera:
            return self._camera.strip()
        from_session = _camera_from_session(storage_path)
        if from_session:
            return from_session
        return os.environ.get(ENV_CAMERA, "").strip()

    def start(self, storage_path: Path) -> None:
        storage_path = Path(storage_path)

        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin is None:
            raise RecorderError(_INSTALL_HINT)

        device = self._resolve_device(storage_path)
        if not device:
            raise RecorderError(_NO_CAMERA_HINT)

        output_path = storage_path / CAPTURE_FILENAME
        command = build_ffmpeg_command(device, output_path, ffmpeg=ffmpeg_bin)

        storage_path.mkdir(parents=True, exist_ok=True)
        # ffmpeg's progress/errors go to stderr; keep a log inside the session
        # folder (which is gitignored) so failed captures can be diagnosed.
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
        self._output_path = output_path

    def stop(self) -> None:
        proc = self._process
        if proc is None:
            self._close_log()
            return

        try:
            if proc.poll() is None:
                self._graceful_stop(proc)
                try:
                    proc.wait(timeout=STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    # ffmpeg did not finalize in time; force-kill as a fallback.
                    proc.kill()
                    try:
                        proc.wait(timeout=STOP_TIMEOUT_SECONDS)
                    except subprocess.TimeoutExpired:  # pragma: no cover
                        pass
        finally:
            self._process = None
            self._close_log()

    def _graceful_stop(self, proc: subprocess.Popen) -> None:
        """Ask ffmpeg to finalize the file by sending 'q' to its stdin."""
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
