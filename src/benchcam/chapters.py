"""Review and re-title chapters without re-watching the video.

``benchcam chapters <id>`` prints a scannable sheet of every chapter — its review
timestamp (where its tag appears in review.mp4), current title, and the transcript
line it derives from — joining three artifacts already on disk:

* ``markers.csv``      chapters (source, label, narration) — the current truth;
* ``chapters.json``    the raw->review timestamp map written by ``benchcam edit``
                       (see :func:`benchcam.editor.write_chapters_json`);
* ``transcript.json``  the full-session transcript (for a source quote fallback).

``--edit`` writes an editable ``chapters.txt`` as two lines per chapter: a
read-only ``#`` context line (review/raw time + source quote) above the editable
entry line ``[<index>@<elapsed>] <TITLE>``, where the title is the WHOLE remainder
of the line (so it may contain any character, and there is no fragile title/comment
delimiter to eat while editing). ``--apply`` reads it back and updates ONLY the
``label`` column in markers.csv, matched by ``marker_index`` and guarded against
staleness (each entry is stamped with the chapter's ``elapsed_seconds`` at
``--edit`` time; a line whose chapter has since moved is skipped, not misapplied).

Reuses the existing readers/writers (``read_markers``/``update_marker``, the
transcript loader) — no new CSV/JSON I/O, and nothing here touches the render,
segment plan, captures, or autochapter's chapter generation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from .autochapter import load_cached_transcript
from .editor import CHAPTERS_JSON_FILENAME
from .markers import MARKERS_FILENAME, read_markers, update_marker

CHAPTERS_TXT_FILENAME = "chapters.txt"

#: Tolerance (seconds) when matching a chapters.txt line's stamped elapsed_seconds
#: against the current markers.csv value — absorbs formatting (90.2 vs 90.200).
_STALE_TOLERANCE = 1e-3

#: Trim the read-only quote in chapters.txt / the sheet to keep lines scannable.
_QUOTE_WIDTH = 70

#: One editable entry line: ``[<index>@<elapsed>] <title>``; the title is the whole
#: remainder of the line. (Read-only context lives on its own ``#`` line above it.)
_LINE_RE = re.compile(r"^\[(\d+)@([-0-9.]+)\]\s+(.*)$")

#: A title longer than this is treated as a probable parse failure (the real
#: incident absorbed a ~130-char comment into a label). Titles applied here are
#: hand-written and do NOT pass through ``sanitize_label``, so this is a generous
#: parse-failure heuristic - NOT an on-screen caption-width limit (a rendering
#: concern that must not be conflated with parsing).
_MAX_TITLE_LEN = 80

#: A hand-edit that lets the read-only context leak onto an entry line yields a
#: title still carrying the context's ``review <t>  raw <t>`` fingerprint. --apply
#: skips such a line (never truncating what you typed). Deliberately narrow: it
#: keys on the timing-label shape (a word then a digit), not on generic characters
#: like ``|`` or ``"`` that a real title might legitimately contain.
_LEAKED_CONTEXT_RE = re.compile(r"\breview\b\s+\d.*\braw\b\s+\d")


class ChaptersError(RuntimeError):
    """Raised for problems reading/writing the chapter review sheet."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

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


def _fmt_ts(seconds: float | None) -> str:
    """``mm:ss.s`` for a timestamp, or ``--:--`` when unavailable."""
    if seconds is None:
        return "--:--"
    seconds = max(seconds, 0.0)
    return f"{int(seconds) // 60:02d}:{seconds % 60:04.1f}"


def load_chapters_json(session_dir: Path | str) -> dict | None:
    """Load ``chapters.json`` (the review-time map), or None if missing/unreadable."""
    import json

    path = Path(session_dir) / CHAPTERS_JSON_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _quote_for(row: dict, segments) -> str:
    """The source line a chapter derives from: its narration, else nearest segment."""
    narration = (row.get("narration") or "").strip()
    if narration:
        return " ".join(narration.split())
    elapsed = _float(row.get("elapsed_seconds"))
    if segments and elapsed is not None:
        nearest = min(segments, key=lambda s: abs(s.start - elapsed))
        return " ".join((nearest.text or "").split())
    return ""


# --------------------------------------------------------------------------- #
# Sheet building (join markers + chapters.json + transcript)
# --------------------------------------------------------------------------- #

def build_sheet_rows(session_dir: Path | str) -> tuple[list[dict], bool]:
    """Build the per-chapter sheet rows and whether review times are available.

    Base list is the labeled markers from markers.csv (current truth), sorted by
    elapsed_seconds. Review time is joined from chapters.json by ``marker_index``
    (``None`` if there's no render yet); the quote comes from ``narration`` or the
    nearest transcript segment.
    """
    session_dir = Path(session_dir)
    rows = read_markers(session_dir / MARKERS_FILENAME)
    labeled = [r for r in rows if (r.get("label") or "").strip()]
    labeled.sort(key=lambda r: (_float(r.get("elapsed_seconds")) is None,
                                _float(r.get("elapsed_seconds")) or 0.0))

    chapters_json = load_chapters_json(session_dir)
    review_by_index: dict[int, float] = {}
    if chapters_json:
        for entry in chapters_json.get("chapters", []) or []:
            idx = _int(entry.get("marker_index"))
            review = _float(entry.get("review_seconds"))
            if idx is not None and review is not None:
                review_by_index[idx] = review

    segments = load_cached_transcript(session_dir)  # None if no transcript.json

    sheet: list[dict] = []
    for ordinal, row in enumerate(labeled, 1):
        idx = _int(row.get("marker_index"))
        sheet.append(
            {
                "ordinal": ordinal,
                "marker_index": idx,
                "elapsed": _float(row.get("elapsed_seconds")),
                "review": review_by_index.get(idx),
                "label": (row.get("label") or "").strip(),
                "source": (row.get("source") or "").strip(),
                "quote": _quote_for(row, segments),
            }
        )
    return sheet, bool(chapters_json)


def format_sheet(sheet: list[dict], has_review: bool, session_id: str) -> str:
    """Render the sheet as a clean, aligned, human-scannable table."""
    header_note = (
        "(review times from chapters.json)"
        if has_review
        else "(no render yet - review times unavailable; run 'benchcam edit')"
    )
    lines = [f"Session {session_id} - {len(sheet)} chapter(s)   {header_note}", ""]
    if not sheet:
        lines.append("  (no titled chapters in markers.csv)")
        return "\n".join(lines)

    # Column widths from the data (title/source), timestamps are fixed-width.
    tw = max((len(r["label"]) for r in sheet), default=5)
    sw = max((len(r["source"]) for r in sheet), default=6)
    lines.append(
        f"  {'#':>2}  {'review':>8}  {'raw':>8}  {'title':<{tw}}  "
        f"{'source':<{sw}}  from"
    )
    for r in sheet:
        quote = r["quote"][:_QUOTE_WIDTH]
        from_col = f'"{quote}"' if quote else "-"
        lines.append(
            f"  {r['ordinal']:>2}  {_fmt_ts(r['review']):>8}  {_fmt_ts(r['elapsed']):>8}  "
            f"{r['label']:<{tw}}  {r['source']:<{sw}}  {from_col}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Editable round-trip (chapters.txt)
# --------------------------------------------------------------------------- #

def write_chapters_txt(session_dir: Path | str, sheet: list[dict]) -> Path:
    """Write the editable ``chapters.txt`` from the sheet rows.

    Two lines per chapter: a read-only ``#`` context line (review/raw time +
    source quote) then the editable entry line
    ``[<marker_index>@<elapsed>] <TITLE>``. The ``[index@elapsed]`` key is the
    stable match key AND the staleness stamp; the TITLE is the whole remainder of
    the entry line (edit it to end-of-line - no trailing delimiter to preserve).
    ``--apply`` ignores every ``#`` line, so the context is safe to keep, change,
    or delete.
    """
    session_dir = Path(session_dir)
    lines = [
        "# benchcam chapters - edit ONLY the title at the end of each",
        "# '[index@elapsed] Title' line. The title runs to the end of the line and",
        "# may contain any character (#, \", |). Each '#' line is READ-ONLY context",
        "# (review/raw time + the source quote), ignored by --apply; leave, change,",
        "# or delete the '#' lines freely.",
        "# Do NOT change the [index@elapsed] key: --apply matches on the index and",
        "# refuses a line whose chapter has since moved (re-run --edit if warned).",
        "#",
    ]
    for row in sheet:
        if row["marker_index"] is None or row["elapsed"] is None:
            continue  # can't stamp a row without a stable key
        review = _fmt_ts(row["review"])
        raw = _fmt_ts(row["elapsed"])
        quote = row["quote"][:_QUOTE_WIDTH]
        lines.append(
            f'# [{row["marker_index"]}] review {review}  raw {raw}  | "{quote}"'
        )
        lines.append(f'[{row["marker_index"]}@{row["elapsed"]:.3f}] {row["label"]}')
    path = session_dir / CHAPTERS_TXT_FILENAME
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def apply_chapters_txt(
    session_dir: Path | str, *, out: Callable[[str], object] = print
) -> tuple[list[tuple[int, str]], int]:
    """Read ``chapters.txt`` and update ONLY the label of each matching marker.

    The title is the whole remainder of an ``[index@elapsed] Title`` entry line
    (``#`` context lines are skipped). Matched by ``marker_index`` and guarded by
    the ``@elapsed`` stamp: a line whose stamped elapsed no longer equals the
    current markers.csv value (chapter moved/renumbered by an intervening
    regeneration) is skipped with a warning rather than applying a title to the
    wrong chapter. A title that still carries the read-only context's fingerprint,
    or is implausibly long, is treated as a parse failure and skipped (never
    truncated). Malformed lines, empty titles and unknown indices are skipped with
    a warning too — markers.csv is never partially corrupted. Nothing but the
    ``label`` cell is ever touched.

    Returns ``(applied, skipped_count)``.
    """
    session_dir = Path(session_dir)
    path = session_dir / CHAPTERS_TXT_FILENAME
    if not path.exists():
        raise ChaptersError(
            f"{path} not found - run 'benchcam chapters --edit' first, edit titles, "
            "then --apply."
        )

    markers_file = session_dir / MARKERS_FILENAME
    elapsed_by_index: dict[int, float] = {}
    for row in read_markers(markers_file):
        idx = _int(row.get("marker_index"))
        elapsed = _float(row.get("elapsed_seconds"))
        if idx is not None and elapsed is not None:
            elapsed_by_index[idx] = elapsed

    applied: list[tuple[int, str]] = []
    skipped = 0
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            out(f"  skip line {lineno}: not a '[index@elapsed] Title' line.")
            skipped += 1
            continue
        idx = int(match.group(1))
        stamp = _float(match.group(2))
        title = match.group(3).strip()
        if stamp is None:
            out(f"  skip line {lineno}: unreadable elapsed stamp for [{idx}].")
            skipped += 1
            continue
        if not title:
            out(f"  skip [{idx}]: empty title.")
            skipped += 1
            continue
        if _LEAKED_CONTEXT_RE.search(title) or len(title) > _MAX_TITLE_LEN:
            out(
                f"  skip [{idx}]: title looks like it absorbed the read-only context "
                f"({len(title)} chars) - re-run --edit and retype just the title."
            )
            skipped += 1
            continue
        if idx not in elapsed_by_index:
            out(f"  skip [{idx}]: no marker with that index in markers.csv.")
            skipped += 1
            continue
        if abs(elapsed_by_index[idx] - stamp) > _STALE_TOLERANCE:
            out(
                f"  skip [{idx}]: chapters.txt is stale (stamped {stamp:.3f}s != "
                f"current {elapsed_by_index[idx]:.3f}s) - re-run --edit."
            )
            skipped += 1
            continue
        update_marker(markers_file, idx, {"label": title})
        applied.append((idx, title))
        out(f"  [{idx}] -> {title!r}")

    out(f"Applied {len(applied)} title(s); skipped {skipped}.")
    if skipped:
        out(
            "  (some lines were skipped - re-run 'benchcam chapters --edit' to "
            "regenerate, retype the titles, and --apply again.)"
        )
    return applied, skipped


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def run_chapters(
    session_dir: Path | str,
    *,
    edit: bool = False,
    apply: bool = False,
    out: Callable[[str], object] = print,
) -> int:
    """Print the sheet, or write/apply ``chapters.txt`` (mutually exclusive modes)."""
    session_dir = Path(session_dir)
    if apply:
        _applied, skipped = apply_chapters_txt(session_dir, out=out)
        return 1 if skipped else 0  # non-zero so scripted use notices skipped lines

    sheet, has_review = build_sheet_rows(session_dir)
    if edit:
        path = write_chapters_txt(session_dir, sheet)
        out(
            f"Wrote {path} ({len(sheet)} chapter(s)). Edit the titles, then: "
            "benchcam chapters --apply"
        )
        if not has_review:
            out(
                "  (no render yet - review times show as --:--; run 'benchcam edit' "
                "to fill them.)"
            )
        return 0

    out(format_sheet(sheet, has_review, session_dir.name))
    return 0
