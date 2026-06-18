"""Collect an externally-recorded video file into the session folder.

Some recorders (e.g. the OBS recorder) let the capture app decide where the
video is written — typically OBS's own recording folder, not the BenchCam
session folder. This module moves that file into the session folder on session
end so each session folder is self-contained (video + ``markers.csv`` +
``session.json`` + ``notes.md`` together).

Design goals (this runs at end-of-session, so it must never lose footage or
crash the session end):

- MOVE, not copy, to avoid duplicating large video files.
- Cross-filesystem safe: a plain rename fails across drives (e.g. video on
  ``C:`` but the session root on an external SSD), so fall back to copy+delete.
- Tolerant of OBS still finalizing the file: briefly poll for the file to appear
  before giving up.
- Degrade gracefully: any failure returns ``None`` (the caller keeps pointing at
  the original external path) and logs a warning — it never raises and never
  deletes the source unless the copy/move into the session folder succeeded.

Stdlib only (``os``, ``shutil``, ``pathlib``, ``time``, ``logging``).
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Callable

DEFAULT_CAPTURE_STEM = "capture"
DEFAULT_WAIT_TIMEOUT = 5.0  # seconds to wait for the file to appear / finalize
DEFAULT_POLL_INTERVAL = 0.25

_LOG = logging.getLogger("benchcam.recorders.collect")


def _wait_for_file(
    path: Path,
    timeout: float,
    interval: float,
    sleep: Callable[[float], None],
) -> bool:
    """Return True once ``path`` exists, polling until ``timeout`` elapses."""
    if interval <= 0:
        interval = 0.01
    waited = 0.0
    while True:
        if path.exists():
            return True
        if waited >= timeout:
            return False
        sleep(interval)
        waited += interval


def _move(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst``, falling back to copy+delete across filesystems.

    A plain ``os.replace`` is atomic and fast on the same filesystem, but raises
    (EXDEV) across drives, so on any OSError we copy then remove the original.
    If the copy fails it propagates (so the caller can clean up and keep the
    source); if only the source-delete fails, the video is already safely in the
    session folder, so that is treated as success.
    """
    try:
        os.replace(os.fspath(src), os.fspath(dst))
        return
    except OSError:
        pass
    shutil.copy2(os.fspath(src), os.fspath(dst))
    try:
        os.remove(os.fspath(src))
    except OSError:
        _LOG.warning(
            "Copied recording into %s but could not delete the original %s; "
            "leaving the original in place.",
            dst,
            src,
        )


def collect_recording(
    source: str | os.PathLike | None,
    storage_path: str | os.PathLike,
    *,
    capture_stem: str = DEFAULT_CAPTURE_STEM,
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    warn: Callable[[str], None] | None = None,
) -> Path | None:
    """Move ``source`` into ``storage_path`` as ``capture<ext>``.

    Returns the new in-folder path on success, or ``None`` on any failure (in
    which case the caller should keep pointing at the original ``source``). Never
    raises for expected failures.
    """
    if warn is None:
        warn = _LOG.warning
    if not source:
        return None

    source = Path(source)
    storage_path = Path(storage_path)

    # Already inside the session folder (e.g. a future in-folder recorder)?
    # Nothing to collect — avoid a pointless self-move.
    try:
        if source.parent.resolve() == storage_path.resolve():
            return source
    except OSError:
        pass

    if not _wait_for_file(source, wait_timeout, poll_interval, sleep):
        warn(
            f"Recording not found at {source} after {wait_timeout:g}s; leaving "
            "the pointer at the original location."
        )
        return None

    dest = storage_path / f"{capture_stem}{source.suffix}"
    try:
        storage_path.mkdir(parents=True, exist_ok=True)
        _move(source, dest)
    except OSError as exc:
        warn(
            f"Could not move recording {source} -> {dest}: {exc}. Leaving the "
            "video at its original location."
        )
        # Remove a partially-written destination so we don't leave a broken file
        # next to the markers; never touch the source on failure.
        if dest.exists() and dest != source:
            try:
                dest.unlink()
            except OSError:
                pass
        return None

    return dest
