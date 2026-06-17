"""Recorder backends for BenchCam.

A recorder is responsible only for capturing audio/video. It must never control
moving hardware or actuators. BenchCam may receive external marker events, but
recorders stay strictly on the capture side.

Start with :class:`NullRecorder` (no real capture) so session and marker
behavior can be tested without any camera, OBS, or ffmpeg dependency.
"""

from __future__ import annotations

from .base import Recorder, RecorderError
from .ffmpeg import FfmpegRecorder
from .null import NullRecorder
from .obs import ObsRecorder

RECORDERS = {
    "null": NullRecorder,
    "obs": ObsRecorder,
    "ffmpeg": FfmpegRecorder,
}


def get_recorder(name: str) -> Recorder:
    """Instantiate a recorder by name (``null``, ``obs``, or ``ffmpeg``)."""
    key = (name or "null").strip().lower()
    try:
        return RECORDERS[key]()
    except KeyError as exc:
        valid = ", ".join(sorted(RECORDERS))
        raise RecorderError(
            f"Unknown recorder {name!r}. Available recorders: {valid}."
        ) from exc


__all__ = [
    "Recorder",
    "RecorderError",
    "NullRecorder",
    "ObsRecorder",
    "FfmpegRecorder",
    "RECORDERS",
    "get_recorder",
]
