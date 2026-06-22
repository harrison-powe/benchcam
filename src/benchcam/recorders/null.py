"""NullRecorder: a recorder that records nothing.

This is the default backend for BenchCam v0. It performs no media capture, which
lets us exercise session creation, the run/end lifecycle, and marker logging
without a camera, OBS, or ffmpeg installed.

If you are capturing video by hand (for example, hitting record in a separate
app), the NullRecorder is also the right choice: BenchCam still logs your
markers and timestamps alongside your manual recording.
"""

from __future__ import annotations

from pathlib import Path

from .base import Recorder


class NullRecorder(Recorder):
    """A no-op recorder used for manual capture and for testing."""

    name = "null"

    def __init__(self) -> None:
        self._running = False
        self._storage_path: Path | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, storage_path: Path) -> None:
        self._storage_path = Path(storage_path)
        self._running = True

    def stop(self, storage_path: Path | None = None) -> None:
        self._running = False
