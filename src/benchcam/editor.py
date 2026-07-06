"""Marker-aware auto-edit: turn a recorded session into a review.mp4 build log.

``benchcam edit`` reads a session's ``capture.*`` video and ``markers.csv`` and
produces ``review.mp4`` in the same folder with NO manual editing:

- The boring stretches between markers are TIMELAPSED (default 8x).
- Around each marker (default 3s before / 5s after) the clip drops to NORMAL
  speed so you can actually see what happened; overlapping/adjacent windows merge
  into one normal-speed segment.
- Original audio is KEPT in the normal-speed windows (narration) and DROPPED in
  the timelapsed stretches (no chipmunk audio).
- Each marker that has a label gets that label burned on screen as a caption
  during its normal-speed window.

ffmpeg is used as an external binary (no Python ffmpeg dependency). The segment
planning and the filtergraph construction here are pure stdlib and are the
unit-tested parts; the actual encode is delegated to ffmpeg.

Additive: this never modifies or deletes ``capture.*`` — it only writes (and
overwrites) ``review.mp4``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .markers import read_markers
from .session import MARKERS_FILENAME, SESSION_FILENAME

OUTPUT_FILENAME = "review.mp4"
#: Fast, low-quality render written by ``edit --preview`` — a distinct filename so
#: it never overwrites the real review.mp4.
PREVIEW_OUTPUT_FILENAME = "review.preview.mp4"
#: Raw->review timestamp map persisted after a render, consumed by
#: ``benchcam chapters`` so titles can be reviewed/edited without the video.
CHAPTERS_JSON_FILENAME = "chapters.json"
OBS_POINTER_FILENAME = "obs_recording.txt"

#: Full-render encode settings (unchanged default).
DEFAULT_PRESET = "veryfast"
DEFAULT_CRF = 20
#: Preview encode settings: ultrafast x264 + a lower quality + a 720p downscale,
#: the biggest honest wall-clock win that still leaves the on-screen chapter tags
#: readable. Only the encode/resolution differ from a full render — the segment
#: plan, timestamps and captions are identical (the scale is a uniform trailing
#: stage applied to the already-composited frame).
PREVIEW_PRESET = "ultrafast"
PREVIEW_CRF = 30
PREVIEW_SCALE_HEIGHT = 720

DEFAULT_PRE = 3.0
DEFAULT_POST = 5.0
DEFAULT_SPEED = 30.0

# Speech-aware speed (opt-in via --vad). Speech spans + marker windows both count
# as normal speed; these tune how speech is turned into normal-speed regions.
DEFAULT_SPEECH_PAD_LEAD = 0.3   # seconds kept before each speech span
DEFAULT_SPEECH_PAD_TRAIL = 0.5  # seconds kept after each speech span
DEFAULT_MERGE_GAP = 1.5         # bridge normal regions closer than this (no strobing)
DEFAULT_MIN_LAPSE = 5.0         # only timelapse gaps longer than this
DEFAULT_VAD_THRESHOLD = 0.5     # silero speech probability threshold
_VAD_SAMPLE_RATE = 16000        # silero-vad expects 16 kHz mono

_INSTALL_HINT = (
    "ffmpeg was not found on PATH. The 'edit' command renders video with ffmpeg. "
    "Install it and try again — Windows: 'winget install Gyan.FFmpeg' (or "
    "https://ffmpeg.org/download.html, then add it to PATH); Linux: "
    "'sudo apt install ffmpeg'; macOS: 'brew install ffmpeg'."
)
_FFPROBE_HINT = (
    "ffprobe was not found on PATH. It ships alongside ffmpeg — installing ffmpeg "
    "provides it (see the ffmpeg install instructions)."
)
_VAD_INSTALL_HINT = (
    "silero-vad is not installed. 'edit --vad' uses silero-vad (with torch) to "
    "detect speech and drive normal-speed regions. Install it on the laptop with: "
    "pip install 'benchcam[vad]' (or: pip install silero-vad). This pulls in torch "
    "and is meant for the laptop, NOT the Raspberry Pi."
)


class EditError(RuntimeError):
    """Raised for problems building or rendering the review clip."""


@dataclass
class Caption:
    """A burned-in label, timed in segment-local seconds (after speed re-timing).

    ``accent`` is an optional leading substring of ``text`` (the chapter number)
    that is re-drawn in a dim colour on top, at the same anchor, so the number
    reads as a muted marker and the label is the emphasis. Empty = single colour.
    """

    text: str
    start: float
    end: float
    accent: str = ""


@dataclass
class Segment:
    """One contiguous slice of the source video in the review timeline."""

    start: float  # source start time (seconds)
    end: float  # source end time (seconds)
    normal: bool  # True = normal speed (keep audio); False = timelapse (drop audio)
    speed: float  # 1.0 for normal segments, else the timelapse factor
    captions: list[Caption] = field(default_factory=list)

    @property
    def source_duration(self) -> float:
        return max(self.end - self.start, 0.0)

    @property
    def output_duration(self) -> float:
        return self.source_duration / self.speed


# --------------------------------------------------------------------------- #
# Reading markers
# --------------------------------------------------------------------------- #

def read_events(session_dir: Path) -> list[tuple[float, str]]:
    """Read (elapsed_seconds, label) pairs from the session's markers.csv."""
    rows = read_markers(Path(session_dir) / MARKERS_FILENAME)
    events: list[tuple[float, str]] = []
    for row in rows:
        try:
            elapsed = float(row.get("elapsed_seconds", ""))
        except (TypeError, ValueError):
            continue
        events.append((elapsed, (row.get("label") or "").strip()))
    return events


#: Written by the Pi GPIO daemon next to markers.csv; the mirror of a marker.
#: A marker forces NORMAL speed; a mute span forces LAPSE speed (see
#: build_segment_plan). Columns: start_seconds,end_seconds,start_wall_time,
#: end_wall_time,span_index,source — only the two seconds columns matter here.
MUTE_SPANS_FILENAME = "mute_spans.csv"


def read_mute_spans(session_dir: Path) -> list[tuple[float, float]]:
    """Read force-timelapse spans ``[(start, end), ...]`` from mute_spans.csv.

    In SOURCE seconds. An absent file yields ``[]`` (unchanged edit behavior),
    the same graceful pattern as a session with no markers — ``read_markers``
    returns ``[]`` for a missing file. Zero-length or inverted spans
    (``end <= start``) are skipped: the daemon can write ``end == start`` on a
    safety close, and those must be no-ops. Unparseable rows are skipped too,
    mirroring ``read_events``'s tolerance.
    """
    spans: list[tuple[float, float]] = []
    for row in read_markers(Path(session_dir) / MUTE_SPANS_FILENAME):
        try:
            start = float(row.get("start_seconds", ""))
            end = float(row.get("end_seconds", ""))
        except (TypeError, ValueError):
            continue
        if end > start:
            spans.append((start, end))
    return spans


# --------------------------------------------------------------------------- #
# Segment planning (pure)
# --------------------------------------------------------------------------- #

def _merge_intervals(
    intervals: list[tuple[float, float]], gap: float
) -> list[tuple[float, float]]:
    """Sort and merge intervals, bridging any two separated by ``<= gap``.

    ``gap=0`` is the plain overlap/adjacency merge (``next.start <= cur.end``);
    a positive gap also absorbs short pauses between otherwise-separate regions.
    """
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + gap:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _subtract_intervals(
    blocks: list[tuple[float, float]], holes: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Return ``blocks`` with every ``holes`` interval removed (interval diff).

    ``blocks`` must be sorted and non-overlapping (as ``_merge_intervals`` yields).
    ``holes`` are normalized first (invalid dropped, then merged) so overlapping
    or unsorted holes are handled. A hole carves its overlap out of any block,
    splitting it; a block untouched by any hole passes through unchanged.
    """
    holes = _merge_intervals([(s, e) for s, e in holes if e > s], 0.0)
    if not holes:
        return list(blocks)
    result: list[tuple[float, float]] = []
    for bs, be in blocks:
        cursor = bs
        for hs, he in holes:  # holes sorted ascending, non-overlapping
            if he <= cursor:
                continue  # hole is entirely before the remaining piece
            if hs >= be:
                break  # this hole (and all later) start at/after the block end
            if hs > cursor:
                result.append((cursor, hs))  # keep the piece before the hole
            cursor = he  # skip past the muted part
            if cursor >= be:
                break
        if cursor < be:
            result.append((cursor, be))  # tail after the last hole
    return result


def build_segment_plan(
    events: list[tuple[float, str]],
    duration: float,
    *,
    pre: float = DEFAULT_PRE,
    post: float = DEFAULT_POST,
    speed: float = DEFAULT_SPEED,
    speech_spans: list[tuple[float, float]] | None = None,
    speech_pad_lead: float = 0.0,
    speech_pad_trail: float = 0.0,
    merge_gap: float = 0.0,
    min_lapse: float = 0.0,
    mute_spans: list[tuple[float, float]] | None = None,
) -> list[Segment]:
    """Compute the ordered review segments for a video of ``duration`` seconds.

    Pure and deterministic so it can be unit-tested without ffmpeg or torch.
    Normal-speed regions come from TWO sources, unioned together (design "B",
    normal = speech OR marker):

    - each marker forces a window ``[t-pre, t+post]`` (clamped to the video);
    - each detected speech span (``speech_spans``, in source seconds) is padded by
      ``speech_pad_lead``/``speech_pad_trail`` — pass ``None`` for marker-only.

    Those intervals are merged (bridging pauses shorter than ``merge_gap``), gaps
    shorter than ``min_lapse`` are absorbed into normal speed (no glitchy micro-
    timelapses), and the remaining gaps are timelapsed at ``speed``. Captions are
    attached afterwards from the markers (see ``_attach_chapter_captions``), so a
    chapter label still persists across whatever segments result.

    ``mute_spans`` (in source seconds) are the mirror of a marker: they FORCE
    timelapse, subtracted from the normal-speed union after the merges, so a mute
    overrides speech, a marker window, and ``min_lapse`` alike. A chapter label
    still draws across the resulting lapse segment via the unchanged retiming map.

    The defaults (``speech_spans=None``, ``merge_gap=0``, ``min_lapse=0``,
    ``mute_spans=None``) reproduce the marker-only tiling exactly, so plain
    ``edit`` is unchanged.
    """
    if duration <= 0:
        return []

    intervals: list[tuple[float, float]] = []
    # Marker-forced windows.
    for raw_time, _label in events:
        t = min(max(raw_time, 0.0), duration)
        ws = max(0.0, t - pre)
        we = min(duration, t + post)
        if we > ws:
            intervals.append((ws, we))
    # Speech-derived windows (padded, then clamped to the video).
    for raw_start, raw_end in speech_spans or []:
        s = max(0.0, min(max(raw_start, 0.0), duration) - speech_pad_lead)
        e = min(duration, min(max(raw_end, 0.0), duration) + speech_pad_trail)
        if e > s:
            intervals.append((s, e))

    # (a) union, bridging short pauses; (b) absorb gaps too short to timelapse.
    blocks = _merge_intervals(_merge_intervals(intervals, merge_gap), min_lapse)

    # Mute spans FORCE timelapse: subtract them from the normal-speed union AFTER
    # the merges above, so a mute overrides speech, a marker window, AND min_lapse
    # (a muted hole shorter than min_lapse still lapses — it can't be re-absorbed
    # here). The walk below turns whatever gap this carves into a _lapse.
    mutes: list[tuple[float, float]] = []
    for raw_start, raw_end in mute_spans or []:
        s = min(max(raw_start, 0.0), duration)
        e = min(max(raw_end, 0.0), duration)
        if e > s:  # drop zero-length / inverted (incl. spans clamped to nothing)
            mutes.append((s, e))
    if mutes:
        blocks = _subtract_intervals(blocks, mutes)

    # (c) Walk the timeline, filling the remaining gaps with timelapse segments.
    # Captions are attached in a second pass because a chapter label spans
    # multiple segments, not just its own normal window.
    segments: list[Segment] = []
    cursor = 0.0
    for ws, we in blocks:
        if ws > cursor:
            segments.append(_lapse(cursor, ws, speed))
        segments.append(Segment(start=ws, end=we, normal=True, speed=1.0))
        cursor = we
    if cursor < duration:
        segments.append(_lapse(cursor, duration, speed))

    _attach_chapter_captions(segments, events, duration, pre=pre)
    return segments


def _lapse(start: float, end: float, speed: float) -> Segment:
    return Segment(start=start, end=end, normal=False, speed=speed, captions=[])


@dataclass(frozen=True)
class _Chapter:
    """A labeled marker as a persistent chapter over a source-time range."""

    number: int  # 1-based ordinal over labeled markers, in time order
    label: str
    src_start: float  # source seconds: this chapter's window start
    src_end: float  # source seconds: next chapter's window start (or duration)


def _chapters(events: list[tuple[float, str]], duration: float, pre: float) -> list[_Chapter]:
    """Turn labeled markers into contiguous, non-overlapping chapters.

    Each chapter is active in SOURCE time from its own window start
    ``max(0, t - pre)`` until the next labeled marker's window start; the last
    runs to ``duration``. Numbering is the 1-based ordinal over labeled markers
    in time order (always contiguous — unlabeled markers are ignored, not counted).
    """
    labeled = sorted(
        (
            (min(max(t, 0.0), duration), label)
            for t, label in events
            if label.strip()
        ),
        key=lambda e: e[0],
    )
    starts = [max(0.0, t - pre) for t, _ in labeled]
    chapters: list[_Chapter] = []
    for i, (_t, label) in enumerate(labeled):
        src_start = starts[i]
        src_end = starts[i + 1] if i + 1 < len(starts) else duration
        if src_end <= src_start:
            continue  # zero-length (markers within `pre` of each other): later wins
        chapters.append(_Chapter(i + 1, label.strip(), src_start, src_end))
    return chapters


def _attach_chapter_captions(
    segments: list[Segment],
    events: list[tuple[float, str]],
    duration: float,
    *,
    pre: float,
) -> None:
    """Distribute each chapter's label across every segment its range overlaps.

    A chapter active over source ``[a, b)`` is drawn on each overlapping segment
    for the sub-range that falls inside it, converted to that segment's LOCAL
    (post-retiming) output time via ``(x - seg.start) / seg.speed`` — so a label
    persists across timelapse segments and stays in sync after the speed-up. The
    number prefix is baked into the caption text (escaped later by drawtext).
    """
    for chapter in _chapters(events, duration, pre):
        accent = f"{chapter.number:02d}"  # dim-overdrawn leading number
        text = f"{accent}{_CAPTION_NUMBER_SEP}{chapter.label}"
        for seg in segments:
            lo = max(chapter.src_start, seg.start)
            hi = min(chapter.src_end, seg.end)
            if hi <= lo:
                continue
            local_start = (lo - seg.start) / seg.speed
            local_end = (hi - seg.start) / seg.speed
            seg.captions.append(
                Caption(text=text, start=local_start, end=local_end, accent=accent)
            )


def describe_plan(
    plan: list[Segment], *, speed: float, marker_count: int
) -> str:
    """A human-readable summary of the plan for a sanity check before encoding."""
    lines = [f"Segment plan ({marker_count} marker(s), timelapse {speed:g}x):"]
    if not plan:
        lines.append("  (empty — no usable video duration)")
        return "\n".join(lines)
    out_total = 0.0
    for i, seg in enumerate(plan, 1):
        out_total += seg.output_duration
        kind = "NORMAL " if seg.normal else f"LAPSE {seg.speed:g}x"
        labels = ", ".join(c.text for c in seg.captions)
        cap = f"  captions: {labels}" if labels else ""
        lines.append(
            f"  {i:>2}. {kind:<9} src {seg.start:7.3f}-{seg.end:7.3f}s "
            f"({seg.source_duration:6.3f}s) -> out {seg.output_duration:6.3f}s{cap}"
        )
    lines.append(f"  estimated review length: {out_total:.1f}s")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Chapter review-timestamp map (persisted for `benchcam chapters`)
# --------------------------------------------------------------------------- #

def _segment_out_bases(plan: list[Segment]) -> list[float]:
    """Cumulative review-timeline start of each segment (its output offset)."""
    bases: list[float] = []
    acc = 0.0
    for seg in plan:
        bases.append(acc)
        acc += seg.output_duration
    return bases


def _review_time_for_src(src: float, plan: list[Segment], bases: list[float]) -> float | None:
    """Review-video time of source instant ``src`` (the caption's local-time map).

    ``review = out_base(seg) + (src - seg.start) / seg.speed`` for the segment
    containing ``src`` — the SAME formula ``_attach_chapter_captions`` uses, so a
    chapter's persisted time equals the moment its tag appears on screen.
    """
    for i, seg in enumerate(plan):
        if seg.start <= src < seg.end:
            return bases[i] + (src - seg.start) / seg.speed
    if plan and src <= plan[0].start:
        return bases[0]
    if plan and src >= plan[-1].end:  # at/after the very end
        return bases[-1] + plan[-1].output_duration
    return None


def chapter_review_records(
    rows: list[dict], plan: list[Segment], duration: float, *, pre: float
) -> list[dict]:
    """Per-chapter ``{marker_index, elapsed_seconds, review_seconds, ...}`` (pure).

    Reuses ``_chapters`` for the authoritative chapter list/numbering (exactly what
    the render draws) and joins each chapter back to its marker row by ordinal, so
    the persisted map carries ``marker_index`` and the raw ``elapsed_seconds``
    alongside the computed ``review_seconds``. Does not recompute or re-render.
    """
    events: list[tuple[float, str]] = []
    labeled_rows: list[tuple[float, dict]] = []
    for row in rows:
        try:
            elapsed = float(row.get("elapsed_seconds", ""))
        except (TypeError, ValueError):
            continue
        label = (row.get("label") or "").strip()
        events.append((elapsed, label))
        if label:
            labeled_rows.append((min(max(elapsed, 0.0), duration), row))
    labeled_rows.sort(key=lambda pair: pair[0])  # same key/order as _chapters

    bases = _segment_out_bases(plan)
    records: list[dict] = []
    seen: dict[float, bool] = {}
    for chapter in _chapters(events, duration, pre):
        if not (1 <= chapter.number <= len(labeled_rows)):
            continue
        row = labeled_rows[chapter.number - 1][1]
        try:
            marker_index = int(row.get("marker_index"))
            elapsed = float(row.get("elapsed_seconds"))
        except (TypeError, ValueError):
            continue
        review = _review_time_for_src(chapter.src_start, plan, bases)
        note = ""
        if review is None:
            review, note = 0.0, "unresolved segment"
        else:
            key = round(review, 3)
            if key in seen:  # two markers sharing an identical window start
                note = "shares review start (approx)"
            seen[key] = True
        records.append(
            {
                "marker_index": marker_index,
                "chapter_number": chapter.number,
                "elapsed_seconds": round(elapsed, 3),
                "review_seconds": round(review, 3),
                "label": chapter.label,
                "source": (row.get("source") or "").strip(),
                "note": note,
            }
        )
    return records


def write_chapters_json(
    session_dir: Path | str,
    plan: list[Segment],
    duration: float,
    *,
    pre: float,
    review_filename: str,
) -> Path:
    """Persist the raw->review chapter map to ``chapters.json`` (atomically-ish).

    An additive artifact for ``benchcam chapters``; it neither affects nor is read
    by the render. Reuses ``read_markers`` for input and mirrors the other
    sidecar writers' temp-then-replace pattern.
    """
    session_dir = Path(session_dir)
    rows = read_markers(session_dir / MARKERS_FILENAME)
    payload = {
        "review_filename": review_filename,
        "chapters": chapter_review_records(rows, plan, duration, pre=pre),
    }
    path = session_dir / CHAPTERS_JSON_FILENAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
    return path


# --------------------------------------------------------------------------- #
# ffmpeg command construction (pure)
# --------------------------------------------------------------------------- #

def _escape_drawtext(text: str) -> str:
    r"""Escape label text for an ffmpeg drawtext ``text=`` value in a filtergraph.

    The value passes through two parser levels (the filtergraph parser, then
    drawtext's own ``:``-separated option parser), so characters special to
    either level must survive both:

    - ``\`` -> ``\\\\`` (literal backslash)
    - ``:`` -> ``\\:``   (drawtext option separator)
    - ``'`` -> ``\'``    (filtergraph quote)
    - ``,`` ``;`` ``[`` ``]`` -> ``\<char>`` (filtergraph separators / pad refs)
    - ``%`` -> ``\%``    (drawtext text expansion; we also pass expansion=none)

    Verified against ffmpeg by rendering labels like ``it's 3:00`` and
    ``path C:\\tmp``.
    """
    text = text.replace("\r", " ").replace("\n", " ")
    out: list[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\\\\\")
        elif ch == ":":
            out.append("\\\\:")
        elif ch == "'":
            out.append("\\'")
        elif ch in ",;[]":
            out.append("\\" + ch)
        elif ch == "%":
            out.append("\\%")
        else:
            out.append(ch)
    return "".join(out)


def _escape_fontfile(fontfile: str) -> str:
    r"""Escape a font path for a drawtext ``fontfile=`` value in a filtergraph.

    Backslashes become forward slashes (valid for ffmpeg on Windows) and the
    drive-letter colon is escaped so it survives both filtergraph parsing levels,
    e.g. ``C:\Windows\Fonts\arial.ttf`` -> ``C\\:/Windows/Fonts/arial.ttf``.
    """
    return str(fontfile).replace("\\", "/").replace(":", "\\\\:")


# Chapter-tag overlay style. Each marker's label persists top-left as a small,
# monospace "engineering terminal" chapter tag, on a semi-transparent box, from
# its window until the next chapter (drawn on timelapse segments too). All values
# are knobs to tune after eyeballing a render; timing is a hard cut (no fade).
_CAPTION_FONTSIZE = 32  # legible at 1080p (~3% of frame height)
_CAPTION_MARGIN = 64  # pixels inset from the top-left corner
_CAPTION_FONTCOLOR = "0xF5F0E8"  # warm off-white label (softer than pure white)
_CAPTION_NUMBER_COLOR = "0x9E948A"  # muted/dim colour for the ordinal number
_CAPTION_BOXCOLOR = "black@0.62"  # semi-transparent backing box for legibility
# Box padding as "vertical|horizontal" (needs an ffmpeg whose drawtext takes a
# string boxborderw, e.g. 8.x); wider sides give the tag horizontal breathing room.
_CAPTION_BOXBORDERW = "16|28"
# Size/position the box from the FONT's line metrics rather than each string's
# tight glyph bbox, so every chapter box is the SAME height (width still varies).
# Without this, labels with descenders ('p', 'g') get taller boxes. Needs ffmpeg
# 8.x drawtext (older builds lack the y_align option).
_CAPTION_Y_ALIGN = "font"
_CAPTION_NUMBER_SEP = " · "  # between the zero-padded chapter number and the label


def _drawtext_filter(caption: Caption, fontfile: str | None) -> str:
    # Escape the commas so the filtergraph parser keeps them inside between().
    enable = f"enable=between(t\\,{caption.start:.3f}\\,{caption.end:.3f})"
    font = f"fontfile={_escape_fontfile(fontfile)}" if fontfile else None

    def _one(text: str, fontcolor: str, *, box: bool) -> str:
        # expansion=none keeps labels literal (no %{...} expansion / "Stray %").
        parts = [f"text={_escape_drawtext(text)}", "expansion=none"]
        if font:
            parts.append(font)
        parts += [f"fontcolor={fontcolor}", f"fontsize={_CAPTION_FONTSIZE}"]
        if box:
            parts += [
                "box=1",
                f"boxcolor={_CAPTION_BOXCOLOR}",
                f"boxborderw={_CAPTION_BOXBORDERW}",
            ]
        # Top-left chapter tag (was bottom-centre). Fixed-pixel margin. y_align=font
        # keeps the box height constant across labels (see _CAPTION_Y_ALIGN); both
        # the label and the number overdraw use it so their anchors stay identical.
        parts += [
            f"x={_CAPTION_MARGIN}",
            f"y={_CAPTION_MARGIN}",
            f"y_align={_CAPTION_Y_ALIGN}",
            enable,
        ]
        return "drawtext=" + ":".join(parts)

    # The full tag (box + off-white label) is drawn once. When an accent (the
    # chapter number) is set, overdraw just that leading substring in a dim colour
    # at the SAME anchor and with no box: monospace makes the digits land exactly
    # on the label's, so only the number changes colour (the label is not redrawn).
    filters = [_one(caption.text, _CAPTION_FONTCOLOR, box=True)]
    if caption.accent:
        filters.append(_one(caption.accent, _CAPTION_NUMBER_COLOR, box=False))
    return ",".join(filters)


#: Every audio segment is pinned to this exact format right before ``concat``.
#: The silent filler (``anullsrc``, input 1) emits 8-bit ``u8`` samples on some
#: ffmpeg builds and has no sample-format option; without this pin, ``concat``
#: negotiates the lowest common format (``u8``) across all segments and
#: down-converts the real narration to 8-bit, baking in quantization noise that
#: is heard as static bursts at every silence->speech seam. Forcing a real float
#: format (and a consistent rate/layout) on both branches keeps the narration at
#: full precision regardless of the ffmpeg build's ``anullsrc`` behaviour.
_AUDIO_SEGMENT_FORMAT = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"


def build_filter_complex(
    plan: list[Segment],
    *,
    fontfile: str | None = None,
    has_audio: bool = True,
    scale_height: int | None = None,
) -> str:
    """Build the filtergraph string for the review clip (pure, testable).

    Input 0 is the capture video; input 1 is a silent ``anullsrc`` used for
    timelapsed (and audio-less) segments so every concatenated segment has a
    matching audio stream. Every audio segment ends with an explicit
    ``aformat`` (see ``_AUDIO_SEGMENT_FORMAT``) so ``concat`` never collapses the
    narration to the filler's 8-bit format.

    ``scale_height`` (used only by ``edit --preview``) appends a single uniform
    downscale to the *already-composited* output — after every trim/setpts/caption
    stage — so it changes resolution only, never a timestamp or the plan. When it
    is ``None`` (every non-preview render) the graph is byte-for-byte unchanged.
    """
    if not plan:
        raise EditError("Cannot build a filtergraph from an empty segment plan.")

    chains: list[str] = []
    concat_inputs: list[str] = []

    for k, seg in enumerate(plan):
        s, e = seg.start, seg.end
        vfilters = [f"trim=start={s:.3f}:end={e:.3f}"]
        if seg.normal:
            vfilters.append("setpts=PTS-STARTPTS")
        else:
            vfilters.append(f"setpts=(PTS-STARTPTS)/{seg.speed:g}")
        # Chapter tags are drawn on BOTH normal and timelapse segments so a label
        # persists through the montage; each segment's captions carry enable-ranges
        # already mapped to this segment's local (post-setpts) output time.
        for cap in seg.captions:
            vfilters.append(_drawtext_filter(cap, fontfile))
        chains.append(f"[0:v]{','.join(vfilters)}[v{k}]")

        if seg.normal and has_audio:
            chains.append(
                f"[0:a]atrim=start={s:.3f}:end={e:.3f},"
                f"asetpts=PTS-STARTPTS,{_AUDIO_SEGMENT_FORMAT}[a{k}]"
            )
        else:
            chains.append(
                f"[1:a]atrim=start=0:end={seg.output_duration:.3f},"
                f"asetpts=PTS-STARTPTS,{_AUDIO_SEGMENT_FORMAT}[a{k}]"
            )

        concat_inputs.append(f"[v{k}][a{k}]")

    # Preview appends a trailing uniform downscale, so the concat feeds an
    # intermediate [vcat] pad that the scale stage renames to [outv]. Without a
    # scale, the concat writes [outv] directly (byte-identical to before).
    video_out = "vcat" if scale_height else "outv"
    concat = "".join(concat_inputs) + f"concat=n={len(plan)}:v=1:a=1[{video_out}][outa]"
    stages = chains + [concat]
    if scale_height:
        stages.append(f"[vcat]scale=-2:{scale_height}:flags=fast_bilinear[outv]")
    return ";".join(stages)


def build_ffmpeg_edit_command(
    input_path: Path | str,
    output_path: Path | str,
    filter_script_path: Path | str,
    *,
    ffmpeg: str = "ffmpeg",
    preset: str = DEFAULT_PRESET,
    crf: int = DEFAULT_CRF,
) -> list[str]:
    """Build the ffmpeg argv that renders the review clip from a filterscript.

    The filtergraph is read from ``filter_script_path`` via
    ``-filter_complex_script`` rather than inline, which sidesteps command-line
    length limits and shell-escaping pain for long graphs. ``preset``/``crf`` are
    the only encode knobs ``edit --preview`` changes; their defaults reproduce the
    full-render argv exactly.
    """
    return [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-i",
        str(input_path),
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex_script",
        str(filter_script_path),
        "-map",
        "[outv]",
        "-map",
        "[outa]",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


# --------------------------------------------------------------------------- #
# Probing + running ffmpeg (thin wrappers, patchable in tests)
# --------------------------------------------------------------------------- #

def probe_duration(path: Path | str, *, ffprobe: str = "ffprobe") -> float:
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise EditError(
            f"ffprobe could not read {path}: {(result.stderr or '').strip()}"
        )
    try:
        return float((result.stdout or "").strip())
    except ValueError as exc:
        raise EditError(f"Could not parse a duration from {path}.") from exc


def probe_has_audio(path: Path | str, *, ffprobe: str = "ffprobe") -> bool:
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return bool((result.stdout or "").strip())


def run_ffmpeg_command(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


#: Sidecar written by the lossless-remux recovery below. A fixed name (per
#: container) so re-runs overwrite it via ``-y`` instead of accumulating copies.
REMUX_SIDECAR_STEM = "capture.remuxed"


def build_remux_command(
    src: Path | str, dst: Path | str, *, ffmpeg: str = "ffmpeg"
) -> list[str]:
    """Lossless stream-copy remux of ``src`` -> ``dst`` (rebuilds the header).

    Pure/side-effect-free so it is unit-testable without spawning ffmpeg. This is
    ``-c copy`` only: no re-encode, byte-identical A/V, just a freshly written
    container index/header — the fix for a capture whose duration header was
    never finalized (e.g. ffmpeg killed when the card filled). ``-y`` overwrites
    the fixed-name sidecar on a re-run.
    """
    return [ffmpeg, "-v", "error", "-y", "-i", str(src), "-c", "copy", str(dst)]


def resolve_capture_duration(
    capture: Path,
    session_dir: Path,
    *,
    ffprobe: str,
    ffmpeg: str,
    out: Callable[[str], object] = print,
) -> tuple[Path, float]:
    """Return ``(capture, duration)``, recovering a missing duration header once.

    Healthy captures return unchanged with no remux. If the duration cannot be
    read (probe raises, or is <= 0), attempt exactly ONE lossless ``-c copy``
    remux to a sidecar in the session folder and re-probe it once:

    * remux + re-probe yields a valid duration -> use the sidecar (original left
      untouched) and print a recovery note;
    * the remux command itself fails -> raise a clean EditError (no second probe);
    * still unparseable after remux -> raise the current clean "Could not parse a
      duration" EditError (terminal — no loop, no second remux).
    """
    try:
        duration = probe_duration(capture, ffprobe=ffprobe)
        if duration > 0:
            return capture, duration
    except EditError:
        pass  # fall through to the one-time remux recovery below

    sidecar = session_dir / f"{REMUX_SIDECAR_STEM}{capture.suffix}"
    remux = run_ffmpeg_command(build_remux_command(capture, sidecar, ffmpeg=ffmpeg))
    if remux.returncode != 0:
        tail = "\n".join((remux.stderr or "").strip().splitlines()[-8:])
        raise EditError(
            f"{capture} has no readable duration and could not be remuxed to "
            f"recover it:\n{tail}"
        )

    # Re-probe the remuxed file exactly once. If it still won't parse, surface
    # the same clean error as before against the ORIGINAL capture (terminal).
    try:
        duration = probe_duration(sidecar, ffprobe=ffprobe)
    except EditError:
        duration = 0.0
    if duration <= 0:
        raise EditError(f"Could not parse a duration from {capture}.")

    out(
        f"{capture.name} had no readable duration (unfinalized header?) — remuxed "
        f"losslessly to {sidecar.name} and using that. Original left untouched."
    )
    return sidecar, duration


# --------------------------------------------------------------------------- #
# Speech detection (silero-vad; optional [vad] extra, lazy import, mockable)
# --------------------------------------------------------------------------- #

def _import_silero():
    """Import torch + silero-vad lazily, mapping a missing dep to a clear error.

    Wrapped in its own function so tests can monkeypatch it without torch/silero
    installed (the stdlib-only core and the Raspberry Pi never import either).
    """
    try:
        import torch  # noqa: F401
        from silero_vad import get_speech_timestamps, load_silero_vad
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise EditError(_VAD_INSTALL_HINT) from exc
    return torch, load_silero_vad, get_speech_timestamps


def _decode_audio_mono_16k(capture: Path | str, ffmpeg: str) -> bytes:
    """Decode the capture's audio to raw 16 kHz mono float32 PCM via ffmpeg.

    A read-only decode of the SOURCE audio (like probe_duration) — it does not
    touch the review's audio filter path. Using ffmpeg (already required) avoids
    torchaudio's flaky backends on Windows.
    """
    cmd = [
        ffmpeg, "-v", "error", "-i", str(capture),
        "-ac", "1", "-ar", str(_VAD_SAMPLE_RATE), "-f", "f32le", "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise EditError(f"ffmpeg could not decode audio for VAD: {detail}")
    return result.stdout


def detect_speech_spans(
    capture: Path | str,
    *,
    threshold: float = DEFAULT_VAD_THRESHOLD,
    ffmpeg: str = "ffmpeg",
    out: Callable[[str], object] = print,
) -> list[tuple[float, float]]:
    """Return speech spans ``[(start, end), ...]`` in SOURCE seconds via silero-vad.

    Pure data out (seconds), so ``build_segment_plan`` stays deterministic and
    torch-free. The torch/silero part goes through ``_import_silero`` and the
    audio read through ffmpeg, both of which tests monkeypatch.
    """
    import numpy as np

    torch, load_silero_vad, get_speech_timestamps = _import_silero()
    pcm = _decode_audio_mono_16k(capture, ffmpeg)
    audio = np.frombuffer(pcm, dtype=np.float32).copy()  # copy: frombuffer is read-only
    wav = torch.from_numpy(audio)
    model = load_silero_vad()
    stamps = get_speech_timestamps(
        wav,
        model,
        sampling_rate=_VAD_SAMPLE_RATE,
        threshold=threshold,
        return_seconds=True,
    )
    spans = [(float(s["start"]), float(s["end"])) for s in stamps]
    out(f"VAD: {len(spans)} speech span(s) detected.")
    return spans


# --------------------------------------------------------------------------- #
# Session / capture resolution
# --------------------------------------------------------------------------- #

def resolve_session_dir(root: Path | str, session: str | None) -> Path:
    """Resolve which session to edit: an explicit id/path, else the newest."""
    root = Path(root)
    if session:
        as_path = Path(session)
        if (as_path / SESSION_FILENAME).exists():
            return as_path
        candidate = root / session
        if (candidate / SESSION_FILENAME).exists():
            return candidate
        raise EditError(
            f"Session {session!r} not found (looked at {as_path} and {candidate})."
        )
    if not root.exists():
        raise EditError(f"No sessions found: {root} does not exist.")
    sessions = [
        d for d in root.iterdir() if d.is_dir() and (d / SESSION_FILENAME).exists()
    ]
    if not sessions:
        raise EditError(f"No sessions found under {root}.")
    return max(sessions, key=lambda d: (d.stat().st_mtime, d.name))


def find_capture(session_dir: Path) -> Path:
    """Find the capture video, falling back to the OBS pointer sidecar."""
    session_dir = Path(session_dir)
    for name in ("capture.mp4", "capture.mkv"):
        candidate = session_dir / name
        if candidate.exists():
            return candidate
    others = sorted(
        p
        for p in session_dir.glob("capture.*")
        if p.suffix.lower() not in (".txt", ".log")
    )
    if others:
        return others[0]
    pointer = session_dir / OBS_POINTER_FILENAME
    if pointer.exists():
        target = Path(pointer.read_text(encoding="utf-8").strip())
        if target.exists():
            return target
        raise EditError(
            f"{pointer} points at {target}, but that file is missing. The OBS "
            "recording could not be found."
        )
    raise EditError(
        f"No capture video found in {session_dir} (looked for capture.mp4/.mkv "
        f"and {OBS_POINTER_FILENAME}). Record a session first."
    )


# Preferred caption fonts, monospace first for the engineering-terminal look.
# Consolas ships with Windows; DejaVu Sans Mono / Menlo cover Linux / macOS. An
# explicit --font still wins; if none of these exist we fall back to drawtext's
# default so a render never dies on a missing font.
_CAPTION_FONT_CANDIDATES_WINDOWS = [
    r"C:\Windows\Fonts\consola.ttf",  # Consolas
    r"C:\Windows\Fonts\consolab.ttf",  # Consolas Bold
    r"C:\Windows\Fonts\cour.ttf",  # Courier New
]
_CAPTION_FONT_CANDIDATES_OTHER = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/Library/Fonts/Courier New.ttf",
]


def _resolve_font(font: str | None) -> str | None:
    """Pick a font file that actually exists, else None (drawtext default).

    The requested ``font`` is preferred but only if it exists; otherwise we fall
    back to a known monospace system font (Consolas on Windows), and finally to no
    explicit fontfile so the render never dies on a missing font.
    """
    candidates: list[str] = []
    if font:
        candidates.append(font)
    if os.name == "nt":
        candidates += _CAPTION_FONT_CANDIDATES_WINDOWS
    else:
        candidates += _CAPTION_FONT_CANDIDATES_OTHER
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None  # let ffmpeg fall back to its default font


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def run_edit(
    session_dir: Path | str,
    *,
    pre: float = DEFAULT_PRE,
    post: float = DEFAULT_POST,
    speed: float = DEFAULT_SPEED,
    font: str | None = None,
    vad: bool = False,
    speech_pad_lead: float = DEFAULT_SPEECH_PAD_LEAD,
    speech_pad_trail: float = DEFAULT_SPEECH_PAD_TRAIL,
    merge_gap: float = DEFAULT_MERGE_GAP,
    min_lapse: float = DEFAULT_MIN_LAPSE,
    vad_threshold: float = DEFAULT_VAD_THRESHOLD,
    preview: bool = False,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    out: Callable[[str], object] = print,
) -> Path:
    """Render the review clip for ``session_dir`` and return its path.

    With ``vad=True`` (opt-in), detected speech spans also drive normal speed
    (design "B": normal = speech OR marker). If silero-vad isn't installed or the
    capture has no audio, this falls back to marker-only speed with a note.

    With ``preview=True`` this renders a fast, low-quality ``review.preview.mp4``
    (720p, ultrafast) for eyeballing chapter/title changes. The segment plan,
    timestamps and captions are identical to a full render — only the encode and
    resolution differ — so a preview is positionally faithful to the final.
    """
    session_dir = Path(session_dir)
    if pre < 0 or post < 0:
        raise EditError("--pre and --post must be >= 0 seconds.")
    if speed <= 1.0:
        raise EditError("--speed must be greater than 1 (it is the timelapse factor).")

    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        raise EditError(_INSTALL_HINT)
    ffprobe = ffprobe or shutil.which("ffprobe")
    if not ffprobe:
        raise EditError(_FFPROBE_HINT)

    capture = find_capture(session_dir)
    # Recover a missing/invalid duration header (e.g. a capture left unfinalized
    # when ffmpeg was killed) via a one-time lossless remux to a sidecar.
    capture, duration = resolve_capture_duration(
        capture, session_dir, ffprobe=ffprobe, ffmpeg=ffmpeg, out=out
    )
    has_audio = probe_has_audio(capture, ffprobe=ffprobe)

    events = read_events(session_dir)
    # Force-timelapse spans written by the Pi daemon (absent file -> [], no-op).
    mute_spans = read_mute_spans(session_dir)

    # Opt-in speech-aware speed. On any problem, fall back to marker-only so a
    # plain (or degraded) edit still renders — never crash the whole command.
    speech_spans: list[tuple[float, float]] | None = None
    if vad:
        if not has_audio:
            out("--vad: capture has no audio track — using marker-only speed.")
        else:
            try:
                speech_spans = detect_speech_spans(
                    capture, threshold=vad_threshold, ffmpeg=ffmpeg, out=out
                )
            except EditError as exc:
                out(f"--vad unavailable ({exc}); using marker-only speed.")
                speech_spans = None

    # merge_gap/min_lapse only shape speech-derived regions; with marker-only they
    # stay 0 so plain 'edit' tiling is byte-for-byte unchanged.
    use_speech = speech_spans is not None
    plan = build_segment_plan(
        events,
        duration,
        pre=pre,
        post=post,
        speed=speed,
        speech_spans=speech_spans,
        speech_pad_lead=speech_pad_lead,
        speech_pad_trail=speech_pad_trail,
        merge_gap=merge_gap if use_speech else 0.0,
        min_lapse=min_lapse if use_speech else 0.0,
        mute_spans=mute_spans,
    )
    if mute_spans:
        out(f"{len(mute_spans)} mute span(s) forced to timelapse.")

    out(f"Editing {capture.name} ({duration:.1f}s, audio={'yes' if has_audio else 'no'}).")
    out(describe_plan(plan, speed=speed, marker_count=len(events)))
    if not events:
        out(
            f"No markers found — rendering a straight {speed:g}x timelapse of the "
            "whole video."
        )

    # --preview: fast low-quality render to a DISTINCT file (never clobbers the
    # real review.mp4). Only the output name, a trailing downscale and the encode
    # preset/crf change — the plan, timestamps and captions above are untouched.
    output = session_dir / (PREVIEW_OUTPUT_FILENAME if preview else OUTPUT_FILENAME)
    if preview:
        out(f"Preview: {PREVIEW_SCALE_HEIGHT}p / {PREVIEW_PRESET} -> {output.name}")
    fontfile = _resolve_font(font)
    filter_complex = build_filter_complex(
        plan,
        fontfile=fontfile,
        has_audio=has_audio,
        scale_height=PREVIEW_SCALE_HEIGHT if preview else None,
    )

    # Pass the (potentially long, escaping-heavy) graph via a temp filterscript
    # rather than inline, then clean it up — only the review clip is left behind.
    script_path: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix="benchcam-edit-", suffix=".ffscript")
        script_path = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(filter_complex)
        command = build_ffmpeg_edit_command(
            capture,
            output,
            script_path,
            ffmpeg=ffmpeg,
            preset=PREVIEW_PRESET if preview else DEFAULT_PRESET,
            crf=PREVIEW_CRF if preview else DEFAULT_CRF,
        )
        result = run_ffmpeg_command(command)
    finally:
        if script_path is not None:
            try:
                script_path.unlink()
            except OSError:
                pass

    if result.returncode != 0:
        tail = "\n".join((result.stderr or "").strip().splitlines()[-8:])
        raise EditError(f"ffmpeg failed to render {output}:\n{tail}")

    # Additive artifact: persist the raw->review timestamp map for `benchcam
    # chapters` (reuses the plan already computed; never affects the render).
    try:
        write_chapters_json(
            session_dir, plan, duration, pre=pre, review_filename=output.name
        )
    except OSError:  # pragma: no cover - a failed sidecar write must not fail edit
        pass
    return output
