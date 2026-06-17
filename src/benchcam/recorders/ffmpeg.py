"""FfmpegRecorder: stub for capturing via ffmpeg.

This backend is NOT implemented yet. The intended approach is to spawn an
``ffmpeg`` subprocess that captures from a camera/microphone device and writes a
media file into the session folder.

TODO (ffmpeg backend):
- TODO: Locate the ffmpeg binary (PATH or a configured path) and verify it runs.
- TODO: Build the input arguments per platform/device:
    * Windows: dshow (e.g. -f dshow -i video="<cam>":audio="<mic>")
    * Linux:   v4l2 + alsa/pulse
    * macOS:   avfoundation
- TODO: Write output to <storage_path>/capture.mkv (mkv survives interrupted
  writes better than mp4).
- TODO: On start(), launch ffmpeg as a subprocess and keep the handle.
- TODO: On stop(), stop ffmpeg gracefully (send 'q' / SIGINT) so the file
  finalizes, then wait for the process to exit.
- TODO: Surface ffmpeg stderr on failure and record the output path + the exact
  command line in session.json for reproducibility.
- TODO: Capture only. Never invoke anything that controls moving hardware.
"""

from __future__ import annotations

from pathlib import Path

from .base import Recorder, RecorderError


class FfmpegRecorder(Recorder):
    """Placeholder recorder for ffmpeg-based capture."""

    name = "ffmpeg"

    def start(self, storage_path: Path) -> None:
        raise RecorderError(
            "FfmpegRecorder is not implemented yet. Use the 'null' recorder for "
            "v0. See benchcam/recorders/ffmpeg.py for the planned implementation."
        )

    def stop(self) -> None:
        # No subprocess to terminate yet; keep stop() safe to call.
        return None
