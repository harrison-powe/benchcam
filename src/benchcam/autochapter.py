"""Generate faithful video chapters from a FULL-session transcript.

``benchcam transcribe`` only captures narration in short windows *around existing
markers*, so an under-marked session has no chapters for the stretches where no
button was pressed. ``benchcam autochapter`` fills that gap: it transcribes the
ENTIRE capture, asks Claude to propose a small set of chapters over the whole
session, and writes them into ``markers.csv`` as ``source=auto`` markers — which
the unchanged ``benchcam edit`` then renders as on-screen chapter tags.

Why this is a SEPARATE command (not part of ``label``): ``label`` is strictly
per-marker and pure; autochapter reasons across the whole session (finding events
the operator never marked). That cross-session reasoning is exactly what must NOT
leak into the per-marker label step.

Design (mirrors transcribe/label):

- Whisper (``[transcribe]`` extra) and the Anthropic SDK (``[label]`` extra) are
  both OPTIONAL and imported lazily (via :func:`benchcam.transcribe.transcribe_audio`
  and :func:`benchcam.label.make_client`), so the stdlib-only core and the Pi never
  pull in torch or anthropic. Runs on the laptop; reads ``ANTHROPIC_API_KEY``.
- The full transcript is cached to ``transcript.json`` in the session folder so a
  re-run doesn't repeat the expensive Whisper pass (``--overwrite`` forces it).
- ANTI-HALLUCINATION is enforced in CODE, not just the prompt. Every proposed
  chapter must cite a transcript line and quote it verbatim; :func:`validate_chapters`
  drops any chapter whose quote is not a real substring of the cited segment and
  snaps each chapter's time to that segment's start. A silent/dead-air stretch has
  no segments to cite, so no chapter can survive there — by construction.
- ADDITIVE, not destructive: real markers (``gpio``/``manual``/…) are ground truth,
  never overwritten or deleted, and WIN over an auto chapter within
  ``--conflict-window`` seconds. Only this command's own prior ``source=auto`` rows
  are regenerated on a re-run (idempotent).

The pure parts (transcript formatting, response parsing, citation validation,
merge) are unit-tested; Whisper and the API are mocked.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable

from . import clock
from .editor import find_capture, probe_has_audio
from .label import make_client, sanitize_label
from .markers import FIELDNAMES, MARKERS_FILENAME, read_markers
from .session import SessionError, load_session
from .transcribe import (
    DEFAULT_LANGUAGE,
    TranscriptSegment,
    resolve_model as resolve_whisper_model,
    transcribe_audio,
)

#: Whole-session chapter reasoning is harder than per-marker labeling, so this
#: defaults to a stronger model than ``label`` (Sonnet 5). Precision is guaranteed
#: in code (see validate_chapters) regardless of model; a bigger model mainly
#: helps RECALL (finding every real event). Verified a valid id in anthropic
#: 0.115.0's Model union. Override with --model / $BENCHCAM_AUTOCHAPTER_MODEL.
DEFAULT_MODEL = "claude-sonnet-5"
ENV_MODEL = "BENCHCAM_AUTOCHAPTER_MODEL"

#: Drop an auto chapter landing within this many seconds of an existing marker
#: (real button presses win) or another accepted auto chapter (dedup).
DEFAULT_CONFLICT_WINDOW = 20.0

#: Source tag for AI-proposed chapters, so they're distinguishable from real
#: presses (``gpio``) and typed markers (``manual``).
AUTO_SOURCE = "auto"

#: Cached full-session transcript (autochapter's own; never touches the per-marker
#: ``narration`` column that ``transcribe`` owns).
TRANSCRIPT_FILENAME = "transcript.json"

#: Generous ceiling for the chapter JSON (a session is a handful of chapters).
MAX_TOKENS = 2000

_FFMPEG_HINT = (
    "ffmpeg/ffprobe were not found on PATH. Whisper uses ffmpeg to decode the "
    "capture's audio for the full-session transcript. Install it — Windows: "
    "'winget install Gyan.FFmpeg'; Linux: 'sudo apt install ffmpeg'; macOS: "
    "'brew install ffmpeg'."
)

SYSTEM_PROMPT = """\
You segment a bench-engineering session into a small set of faithful chapters for
a video chapter list, using ONLY a timestamped transcript of what the engineer
said. The transcript is the sole source of truth.

You are given the transcript as numbered lines:
  [<index>] <mm:ss> <seconds>s: <spoken text>

Return ONLY a JSON array (no prose, no code fences). Each element:
  {
    "segment": <the [index] of the transcript line this chapter comes from>,
    "quote": "<a short phrase copied VERBATIM from that exact line>",
    "at_seconds": <the seconds value of that line>,
    "title": "<terse chapter title, Title Case, 2-4 words>"
  }

HARD RULES:
- Create a chapter ONLY for a distinct event EXPLICITLY described in the
  transcript. Every chapter MUST cite the transcript line ("segment") and copy a
  short verbatim "quote" from that same line. If you cannot quote a line, do not
  create the chapter.
- Do NOT invent chapters to fill gaps, silence, or dead air. Stretches with no
  meaningful narration get NO chapter. Fewer, accurate chapters beat more.
- Merge rambling about one event into ONE chapter; never emit two chapters for
  the same event.
- Title: 2-4 words, Title Case, technical build-log register, describing WHAT
  HAPPENS (not the speaker's words). No trailing punctuation. Preserve real
  casing of acronyms/versions (e.g. CAN, R4.11).

TEMPORAL RULE:
- A chapter marks WHEN an event first happens, not when it is later mentioned.
- If a line refers BACK to an earlier event (e.g. "that worked", "we already got
  it spinning", or "first spin achieved" spoken long after the spin itself), it is
  RETROSPECTIVE narration: do NOT create a new chapter for it, and NEVER reuse the
  earlier event's title for it.
- Title each chapter from the moment the event actually occurs in the transcript.
- Summary/recap narration near the end of the session is a wrap-up: title it as
  such (e.g. "Session Wrap-Up"), not as a repeat of an earlier milestone.
- This rule applies ONLY to genuinely retrospective lines. Present-tense narration
  AS an event happens ("first spin first spin" said while it spins) is NOT
  retrospective — title it normally. Do not suppress a real event's own chapter.
"""


class AutochapterError(RuntimeError):
    """Raised for problems generating chapters from a full-session transcript."""


# --------------------------------------------------------------------------- #
# Model name resolution
# --------------------------------------------------------------------------- #

def resolve_model(model: str | None) -> str:
    """Pick the Claude model: explicit flag, else $BENCHCAM_AUTOCHAPTER_MODEL, else default."""
    if model:
        return model
    return os.environ.get(ENV_MODEL) or DEFAULT_MODEL


# --------------------------------------------------------------------------- #
# Full-session transcript cache (transcript.json)
# --------------------------------------------------------------------------- #

def transcript_path(session_dir: Path | str) -> Path:
    return Path(session_dir) / TRANSCRIPT_FILENAME


def load_cached_transcript(session_dir: Path | str) -> list[TranscriptSegment] | None:
    """Load the cached transcript, or ``None`` if it is missing/unreadable.

    A present-but-empty cache returns ``[]`` (a legitimately silent capture), which
    the caller treats as "cached" so it is not re-transcribed every run.
    """
    path = transcript_path(session_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        return None
    segments: list[TranscriptSegment] = []
    for item in data["segments"]:
        try:
            segments.append(
                TranscriptSegment(
                    float(item["start"]), float(item["end"]), str(item["text"]).strip()
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return segments


def save_transcript(
    session_dir: Path | str,
    segments: list[TranscriptSegment],
    *,
    model: str,
    language: str,
) -> Path:
    """Write the full transcript to ``transcript.json`` (atomically-ish)."""
    path = transcript_path(session_dir)
    payload = {
        "model": model,
        "language": language,
        "segments": [
            {"start": s.start, "end": s.end, "text": s.text} for s in segments
        ],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
    return path


# --------------------------------------------------------------------------- #
# Prompt formatting (pure, unit-tested)
# --------------------------------------------------------------------------- #

def _mmss(seconds: float) -> str:
    total = max(int(seconds), 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def format_transcript_for_prompt(segments: list[TranscriptSegment]) -> str:
    """Render segments as numbered, timestamped lines. Index == list index.

    Empty-text segments are skipped (nothing to cite), but the surviving lines
    keep their true list index so :func:`validate_chapters` can resolve a cite.
    """
    lines: list[str] = []
    for index, seg in enumerate(segments):
        text = " ".join((seg.text or "").split())
        if not text:
            continue
        lines.append(f"[{index}] {_mmss(seg.start)} {seg.start:.1f}s: {text}")
    return "\n".join(lines)


def _user_message(transcript_text: str) -> str:
    return (
        "Transcript (one line per spoken segment):\n\n"
        f"{transcript_text}\n\n"
        "Return the JSON array of chapters now."
    )


# --------------------------------------------------------------------------- #
# Response parsing (pure, tolerant, unit-tested)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ProposedChapter:
    """One chapter as returned by the model (unvalidated)."""

    segment: int | None
    quote: str
    at_seconds: float | None
    title: str


def _extract_json_array(text: str) -> str | None:
    """Return the outermost ``[...]`` substring, ignoring prose / code fences."""
    if not text:
        return None
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_chapters_response(text: str) -> list[ProposedChapter]:
    """Parse the model's JSON array into ProposedChapters (tolerant, no I/O)."""
    raw = _extract_json_array(text)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    chapters: list[ProposedChapter] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        chapters.append(
            ProposedChapter(
                segment=_as_int(item.get("segment")),
                quote=str(item.get("quote") or ""),
                at_seconds=_as_float(item.get("at_seconds")),
                title=str(item.get("title") or ""),
            )
        )
    return chapters


# --------------------------------------------------------------------------- #
# Citation validation — the load-bearing anti-hallucination guarantee (pure)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ValidChapter:
    """A chapter whose citation resolved to real transcript text."""

    at_seconds: float
    title: str
    quote: str
    segment: int


def _norm(text: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse whitespace (for matching)."""
    collapsed = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return " ".join(collapsed.split())


def validate_chapters(
    proposed: list[ProposedChapter],
    segments: list[TranscriptSegment],
    *,
    neighbor: int = 1,
) -> list[ValidChapter]:
    """Keep only chapters whose citation resolves to real transcript text.

    For each proposed chapter:
    - ``segment`` must index a real transcript line (else dropped).
    - the ``quote`` (normalized) must be a substring of the cited line's text, or
      of an immediate +-``neighbor`` line (tolerates an off-by-one cite). If the
      quote appears in no real segment, the chapter is HALLUCINATED and dropped.
    - the chapter's time is SNAPPED to the matched segment's ``start`` — the
      citation is authoritative for timing, so a chapter can only ever land on a
      real spoken segment. A dead-air gap has no segments, so nothing survives
      there, by construction.
    - the title is run through the shared ``label.sanitize_label`` so it matches
      the on-screen tag style exactly; an empty title drops the chapter.
    """
    seg_norms = [_norm(seg.text) for seg in segments]
    valid: list[ValidChapter] = []
    for chapter in proposed:
        if chapter.segment is None or not (0 <= chapter.segment < len(segments)):
            continue
        quote_norm = _norm(chapter.quote)
        if not quote_norm:
            continue
        lo = max(0, chapter.segment - neighbor)
        hi = min(len(segments) - 1, chapter.segment + neighbor)
        matched = next(
            (idx for idx in range(lo, hi + 1) if quote_norm in seg_norms[idx]), None
        )
        if matched is None:
            continue  # quote is not real transcript text -> hallucinated
        title = sanitize_label(chapter.title)
        if not title:
            continue
        valid.append(
            ValidChapter(
                at_seconds=segments[matched].start,
                title=title,
                quote=" ".join((chapter.quote or "").split()),
                segment=matched,
            )
        )
    return valid


# --------------------------------------------------------------------------- #
# Merge into markers (real markers win; source=auto; idempotent) (pure)
# --------------------------------------------------------------------------- #

def _is_auto(source: str | None) -> bool:
    """True if a marker is one of this command's own auto chapters."""
    return AUTO_SOURCE in [tag for tag in (source or "").split("+") if tag]


@dataclass(frozen=True)
class MergePlan:
    """The file rewrite: existing rows to keep, plus new auto chapters to add."""

    protected: list[dict]
    new_auto: list[ValidChapter]


def merge_chapters(
    existing_rows: list[dict],
    chapters: list[ValidChapter],
    *,
    conflict_window: float = DEFAULT_CONFLICT_WINDOW,
) -> MergePlan:
    """Decide which chapters to write, preserving real markers (no I/O).

    - Protected = every existing marker that is NOT one of our own auto chapters
      (``gpio``/``manual``/``external``/…). These are ground truth: kept verbatim,
      never overwritten or deleted.
    - A chapter within ``conflict_window`` seconds of a protected marker is dropped
      (the real press wins). A chapter within ``conflict_window`` of an
      already-accepted chapter is dropped (auto-vs-auto dedup).
    - Prior ``source=auto`` rows are NOT protected: they are regenerated, so a
      re-run replaces them instead of piling up duplicates.
    """
    protected = [row for row in existing_rows if not _is_auto(row.get("source"))]
    protected_times: list[float] = []
    for row in protected:
        seconds = _as_float(row.get("elapsed_seconds"))
        if seconds is not None:
            protected_times.append(seconds)

    accepted: list[ValidChapter] = []
    for chapter in sorted(chapters, key=lambda c: c.at_seconds):
        if any(abs(chapter.at_seconds - t) < conflict_window for t in protected_times):
            continue  # a real marker already covers this moment
        if any(abs(chapter.at_seconds - a.at_seconds) < conflict_window for a in accepted):
            continue  # collapse near-duplicate auto chapters
        accepted.append(chapter)
    return MergePlan(protected=protected, new_auto=accepted)


def _write_merged_markers(
    session_dir: Path, plan: MergePlan, *, baseline_iso: str | None
) -> None:
    """Rewrite markers.csv = protected rows (verbatim) + new auto chapters.

    Protected rows keep their exact columns and indices; auto chapters get fresh
    indices above the current maximum (robust against gaps). The file is sorted
    chronologically for readability — ``edit`` reads only (elapsed, label), so row
    order is cosmetic.
    """
    markers_file = Path(session_dir) / MARKERS_FILENAME
    rows: list[dict] = [dict(row) for row in plan.protected]

    existing_indices = [
        idx for idx in (_as_int(r.get("marker_index")) for r in rows) if idx is not None
    ]
    next_index = (max(existing_indices) + 1) if existing_indices else 1

    baseline = None
    if baseline_iso:
        try:
            baseline = clock.from_iso(baseline_iso)
        except (TypeError, ValueError):
            baseline = None

    for chapter in plan.new_auto:
        wall = ""
        if baseline is not None:
            wall = clock.to_iso(baseline + timedelta(seconds=chapter.at_seconds))
        rows.append(
            {
                "marker_index": next_index,
                "elapsed_seconds": f"{chapter.at_seconds:.3f}",
                "wall_time": wall,
                "source": AUTO_SOURCE,
                "label": chapter.title,
                "narration": chapter.quote,
            }
        )
        next_index += 1

    rows.sort(key=lambda r: (_as_float(r.get("elapsed_seconds")) is None,
                             _as_float(r.get("elapsed_seconds")) or 0.0))
    with markers_file.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in FIELDNAMES})


def _session_baseline_iso(session_dir: Path) -> str | None:
    """The wall-time baseline (run start, else creation) for auto marker stamps."""
    try:
        session = load_session(Path(session_dir))
    except SessionError:
        return None
    return session.started_wall_time or session.created_wall_time


# --------------------------------------------------------------------------- #
# Anthropic call (reuses label's client; mocked in tests)
# --------------------------------------------------------------------------- #

def propose_chapters(client, transcript_text: str, model: str) -> str:
    """Ask the model for the chapter JSON; return its raw text."""
    # NOTE: no ``temperature`` — newer models (e.g. claude-sonnet-5) reject it
    # ("deprecated for this model"). Determinism isn't relied on anyway: the
    # citation validator, not sampling, is what guarantees faithful chapters.
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _user_message(transcript_text)}],
    )
    parts = [
        getattr(block, "text", "")
        for block in getattr(response, "content", []) or []
        if getattr(block, "type", None) == "text"
    ]
    return "".join(parts).strip()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def _transcribe_full(
    session_dir: Path,
    whisper_model: str | None,
    language: str,
    out: Callable[[str], object],
) -> list[TranscriptSegment] | None:
    """Transcribe the WHOLE capture (returns None if there's no audio track)."""
    capture = find_capture(session_dir)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise AutochapterError(_FFMPEG_HINT)
    if not probe_has_audio(capture):
        out(
            f"{capture} has no audio track — nothing to transcribe for chapters. "
            "(Record with a microphone to use autochapter.)"
        )
        return None
    model_name = resolve_whisper_model(whisper_model)
    out(
        f"Transcribing the full capture {capture.name} with Whisper {model_name!r} "
        f"(language={language or 'auto'}); this can take a while..."
    )
    return transcribe_audio(capture, model_name, language=language)


def run_autochapter(
    session_dir: Path | str,
    *,
    model: str | None = None,
    whisper_model: str | None = None,
    language: str = DEFAULT_LANGUAGE,
    conflict_window: float = DEFAULT_CONFLICT_WINDOW,
    overwrite: bool = False,
    dry_run: bool = False,
    out: Callable[[str], object] = print,
) -> list[ValidChapter]:
    """Propose chapters from the full transcript and merge them into markers.csv.

    Returns the auto chapters written (or, with ``dry_run``, that would be). The
    full transcript is cached to ``transcript.json`` (even on a dry run, so the
    next real run is instant); ``--overwrite`` re-runs Whisper. Real markers are
    always preserved; only prior ``source=auto`` chapters are regenerated.
    """
    session_dir = Path(session_dir)

    # 1) Full-session transcript (cached unless --overwrite).
    segments = None if overwrite else load_cached_transcript(session_dir)
    if segments is None:
        segments = _transcribe_full(session_dir, whisper_model, language, out)
        if segments is None:
            return []  # no audio; nothing to do
        save_transcript(
            session_dir,
            segments,
            model=resolve_whisper_model(whisper_model),
            language=language,
        )
        out(f"Cached full transcript ({len(segments)} segment(s)) to {TRANSCRIPT_FILENAME}.")
    else:
        out(
            f"Using cached transcript ({len(segments)} segment(s)); "
            "pass --overwrite to re-transcribe."
        )

    transcript_text = format_transcript_for_prompt(segments)
    if not transcript_text.strip():
        out("Transcript has no usable narration — no chapters to propose.")
        return []

    # 2) Ask Claude for chapters (reuses label's key/SDK validation).
    model_name = resolve_model(model)
    client = make_client()
    out(
        f"Proposing chapters with {model_name!r}"
        + (" (dry run — markers.csv will not be written)." if dry_run else ".")
    )
    raw = propose_chapters(client, transcript_text, model_name)
    proposed = parse_chapters_response(raw)

    # 3) Validate citations — the anti-hallucination guarantee.
    valid = validate_chapters(proposed, segments)
    out(
        f"{len(proposed)} proposed, {len(valid)} survived transcript-citation checks."
    )
    if not valid:
        out("No transcript-supported chapters found — nothing written.")
        return []

    # 4) Merge, with real markers winning on conflict.
    existing_rows = read_markers(session_dir / MARKERS_FILENAME)
    plan = merge_chapters(existing_rows, valid, conflict_window=conflict_window)
    dropped = len(valid) - len(plan.new_auto)

    for chapter in plan.new_auto:
        verb = "would add" if dry_run else "add"
        out(f"  {verb} [{_mmss(chapter.at_seconds)}] {chapter.title!r}  <- {chapter.quote!r}")
    if dropped:
        out(
            f"  ({dropped} chapter(s) dropped: within {conflict_window:g}s of an "
            "existing marker or another chapter.)"
        )

    if dry_run:
        out(
            f"Dry run: {len(plan.new_auto)} chapter(s) proposed, "
            f"{MARKERS_FILENAME} unchanged."
        )
        return plan.new_auto

    _write_merged_markers(
        session_dir, plan, baseline_iso=_session_baseline_iso(session_dir)
    )
    out(
        f"Wrote {len(plan.new_auto)} auto chapter(s) to {MARKERS_FILENAME} "
        f"(source={AUTO_SOURCE}); {len(plan.protected)} existing marker(s) preserved."
    )
    return plan.new_auto
