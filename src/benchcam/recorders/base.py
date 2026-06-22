"""Recorder interface.

A recorder captures media for a session. The interface is intentionally tiny:
``start`` when recording begins, ``stop`` when it ends. Implementations should be
safe to call ``stop`` even if ``start`` failed or was never called.

IMPORTANT: Recorders capture only. They must never command actuators or any
moving hardware.
"""

from __future__ import annotations

from pathlib import Path


class RecorderError(RuntimeError):
    """Raised when a recorder cannot perform a requested action."""


class Recorder:
    """Base class for all recorder backends."""

    #: Short, stable identifier used in session.json and the CLI.
    name: str = "base"

    def start(self, storage_path: Path) -> None:
        """Begin capturing media into ``storage_path``.

        ``storage_path`` is the session folder. Implementations decide the media
        filename(s) within it.
        """
        raise NotImplementedError

    def stop(self, storage_path: Path | None = None) -> None:
        """Stop capturing media. Safe to call even if never started.

        ``storage_path`` (the session folder) is optional and lets a recorder
        stop a capture it did not start in this process — e.g. the ffmpeg
        recorder persists its subprocess PID into the session folder so that
        ``benchcam end`` (a separate process from ``benchcam run``) can find and
        terminate it. In-process callers can omit it.
        """
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"{type(self).__name__}(name={self.name!r})"
