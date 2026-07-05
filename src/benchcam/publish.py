"""Export a paste-ready YouTube chapter block from a rendered session.

``benchcam publish <id>`` turns the ``chapters.json`` written by ``benchcam edit``
(a raw->review timestamp map) into the timestamp block YouTube wants in a video
description. It uses the REVIEW timestamps (each chapter's position in review.mp4),
not the raw capture times, and enforces YouTube's chapter rules — reporting every
adjustment rather than silently emitting an invalid block:

* the first line must be ``0:00`` — the earliest chapter is re-anchored to 0:00
  (its own title kept) and the move is noted;
* chapters must be >=10s apart — a chapter less than 10s after the previous is
  DROPPED (the first kept, the later warned), never merged;
* YouTube needs >=3 chapters — fewer still emit, with a warning.

Read-only: it only READS chapters.json + markers.csv and writes its own
``youtube_chapters.txt``. It never uploads, touches the video, or edits markers —
the review gate is deliberate (no auto-upload). Console/file output is ASCII-only
for Windows cp1252 console safety.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .chapters import load_chapters_json
from .editor import CHAPTERS_JSON_FILENAME
from .markers import MARKERS_FILENAME, read_markers

YOUTUBE_CHAPTERS_FILENAME = "youtube_chapters.txt"

#: YouTube renders chapters only with at least this many timestamps.
MIN_CHAPTERS = 3
#: YouTube rejects the block if any two timestamps are closer than this.
MIN_GAP_SECONDS = 10


class PublishError(RuntimeError):
    """Raised for problems exporting the YouTube chapter block."""


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_timestamp(seconds: float) -> str:
    """Floor to whole seconds and format ``M:SS`` (``H:MM:SS`` at/over an hour)."""
    total = int(math.floor(max(seconds, 0.0)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@dataclass
class PublishBlock:
    """The paste-ready block plus what was adjusted/warned building it."""

    lines: list[str] = field(default_factory=list)
    adjustments: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_chapter_block(data: dict, marker_rows: list[dict]) -> PublishBlock:
    """Build the YouTube chapter block from chapters.json + current marker titles.

    ``data`` is the parsed chapters.json; ``marker_rows`` is markers.csv (for the
    current ``label`` per ``marker_index``, so a title edited since the render is
    reflected). Pure — no I/O. Applies re-anchor-first, then the <10s dedup, then
    the <3 check, recording adjustments/warnings rather than emitting invalid output.
    """
    title_by_index: dict[int, str] = {}
    for row in marker_rows:
        idx = _int(row.get("marker_index"))
        if idx is not None:
            title_by_index[idx] = (row.get("label") or "").strip()

    chapters: list[tuple[float, str]] = []
    for entry in data.get("chapters") or []:
        review = _float(entry.get("review_seconds"))
        if review is None:
            continue
        idx = _int(entry.get("marker_index"))
        title = (
            title_by_index.get(idx, "")
            or (entry.get("label") or "").strip()
            or f"Chapter {entry.get('chapter_number') or (len(chapters) + 1)}"
        )
        chapters.append((review, title))

    if not chapters:
        raise PublishError(
            "chapters.json has no chapters with review times - re-run 'benchcam edit'."
        )

    chapters.sort(key=lambda c: c[0])
    floored = [(int(math.floor(max(rev, 0.0))), title) for rev, title in chapters]

    # Re-anchor the earliest chapter to 0:00 (BEFORE the dedup, so a long pre-first
    # lapse can't cause a false <10s drop). Its own title stays.
    adjustments: list[str] = []
    if floored[0][0] > 0:
        old = floored[0][0]
        adjustments.append(
            f"First chapter '{floored[0][1]}' was at {_fmt_timestamp(old)}; moved to "
            "0:00 (YouTube requires the first chapter at 0:00)."
        )
        floored[0] = (0, floored[0][1])

    # Keep the first of any pair closer than MIN_GAP_SECONDS; drop + warn the later.
    kept: list[tuple[int, str]] = []
    warnings: list[str] = []
    for stamp, title in floored:
        if not kept:
            kept.append((stamp, title))
        elif stamp - kept[-1][0] >= MIN_GAP_SECONDS:
            kept.append((stamp, title))
        else:
            warnings.append(
                f"Dropped '{title}' at {_fmt_timestamp(stamp)}: less than "
                f"{MIN_GAP_SECONDS}s after '{kept[-1][1]}' at "
                f"{_fmt_timestamp(kept[-1][0])} (YouTube would reject the block)."
            )

    if len(kept) < MIN_CHAPTERS:
        warnings.append(
            f"Only {len(kept)} chapter(s) in the block; YouTube needs at least "
            f"{MIN_CHAPTERS} to display chapters."
        )

    lines = [f"{_fmt_timestamp(stamp)} {title}" for stamp, title in kept]
    return PublishBlock(lines=lines, adjustments=adjustments, warnings=warnings)


def run_publish(
    session_dir: Path | str, *, out: Callable[[str], object] = print
) -> int:
    """Print the YouTube chapter block and write it to ``youtube_chapters.txt``.

    Fails with a clear error if there's no chapters.json (no render yet). The
    file contains ONLY the paste-ready block; adjustments/warnings go to the
    console. Read-only w.r.t. markers.csv/chapters.json/the video.
    """
    session_dir = Path(session_dir)
    data = load_chapters_json(session_dir)
    if data is None:
        raise PublishError(
            f"No {CHAPTERS_JSON_FILENAME} in {session_dir} - run 'benchcam edit' "
            "first (the render writes chapters.json with the review-video timestamps)."
        )

    rows = read_markers(session_dir / MARKERS_FILENAME)
    block = build_chapter_block(data, rows)

    for line in block.lines:
        out(line)
    if block.adjustments or block.warnings:
        out("")
    for message in block.adjustments:
        out(f"note: {message}")
    for message in block.warnings:
        out(f"warning: {message}")

    path = session_dir / YOUTUBE_CHAPTERS_FILENAME
    path.write_text("\n".join(block.lines) + "\n", encoding="utf-8")
    out("")
    out(f"Wrote {path} ({len(block.lines)} chapter line(s)).")
    return 0
