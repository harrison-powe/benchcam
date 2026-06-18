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

# After a cross-filesystem copy, the writer (e.g. OBS) may still hold the source
# file handle for a moment, so deleting the original can fail transiently. Retry
# a few times over a couple of seconds before giving up.
DEFAULT_DELETE_RETRIES = 5
DEFAULT_DELETE_DELAY = 0.5  # seconds between delete attempts (~2s over 5 tries)

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


def _delete_with_retry(
    path: Path,
    *,
    retries: int,
    delay: float,
    sleep: Callable[[float], None],
) -> bool:
    """Delete ``path``, retrying to give the writer time to release the handle.

    Returns True if the file is gone (deleted now or already absent), False if
    every attempt failed. Never raises.
    """
    for attempt in range(max(retries, 1)):
        try:
            os.remove(os.fspath(path))
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if attempt < retries - 1:
                sleep(delay)
    return False


def _move(
    src: Path,
    dst: Path,
    *,
    delete_retries: int,
    delete_delay: float,
    sleep: Callable[[float], None],
    warn: Callable[[str], None],
) -> None:
    """Move ``src`` to ``dst``, falling back to copy+delete across filesystems.

    A plain ``os.replace`` is atomic and fast on the same filesystem, but raises
    (EXDEV) across drives, so on any OSError we copy then remove the original.
    If the copy fails it propagates (so the caller can clean up and keep the
    source). The original-delete is retried (the writer, e.g. OBS, may still hold
    the handle just after StopRecord); if all retries fail the copy is still safe
    in the session folder, so it is treated as success and the original is just
    left in place with a warning.
    """
    try:
        os.replace(os.fspath(src), os.fspath(dst))
        return
    except OSError:
        pass
    shutil.copy2(os.fspath(src), os.fspath(dst))
    if not _delete_with_retry(
        src, retries=delete_retries, delay=delete_delay, sleep=sleep
    ):
        warn(
            f"Copied recording into {dst} but could not delete the original "
            f"{src} after {delete_retries} attempts; leaving the original in "
            "place."
        )


def collect_recording(
    source: str | os.PathLike | None,
    storage_path: str | os.PathLike,
    *,
    capture_stem: str = DEFAULT_CAPTURE_STEM,
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    delete_retries: int = DEFAULT_DELETE_RETRIES,
    delete_delay: float = DEFAULT_DELETE_DELAY,
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
        _move(
            source,
            dest,
            delete_retries=delete_retries,
            delete_delay=delete_delay,
            sleep=sleep,
            warn=warn,
        )
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
