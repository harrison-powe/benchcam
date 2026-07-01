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
OBS_POINTER_FILENAME = "obs_recording.txt"

DEFAULT_PRE = 3.0
DEFAULT_POST = 5.0
DEFAULT_SPEED = 8.0

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


class EditError(RuntimeError):
    """Raised for problems building or rendering the review clip."""


@dataclass
class Caption:
    """A burned-in label, timed in segment-local seconds (after speed re-timing)."""

    text: str
    start: float
    end: float


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


# --------------------------------------------------------------------------- #
# Segment planning (pure)
# --------------------------------------------------------------------------- #

def build_segment_plan(
    events: list[tuple[float, str]],
    duration: float,
    *,
    pre: float = DEFAULT_PRE,
    post: float = DEFAULT_POST,
    speed: float = DEFAULT_SPEED,
) -> list[Segment]:
    """Compute the ordered review segments for a video of ``duration`` seconds.

    Pure and deterministic so it can be unit-tested without ffmpeg. Each marker
    gets a normal-speed window ``[t-pre, t+post]`` (clamped to the video);
    overlapping or adjacent windows merge; the gaps between windows are
    timelapsed at ``speed``. With no markers the whole video is one timelapse.
    """
    if duration <= 0:
        return []

    # Clamp marker times into the video and build normal-speed windows.
    windows: list[tuple[float, float, list[tuple[float, str]]]] = []
    for raw_time, label in events:
        t = min(max(raw_time, 0.0), duration)
        ws = max(0.0, t - pre)
        we = min(duration, t + post)
        if we <= ws:
            continue
        windows.append((ws, we, [(t, label)]))

    windows.sort(key=lambda w: w[0])

    # Merge overlapping/adjacent windows (next.start <= current.end).
    merged: list[tuple[float, float, list[tuple[float, str]]]] = []
    for ws, we, marks in windows:
        if merged and ws <= merged[-1][1]:
            cs, ce, cmarks = merged[-1]
            merged[-1] = (cs, max(ce, we), cmarks + marks)
        else:
            merged.append((ws, we, marks))

    # Walk the timeline, filling gaps with timelapse segments. Captions are
    # attached in a second pass (see _attach_chapter_captions) because a chapter
    # label now spans multiple segments, not just its own normal window.
    segments: list[Segment] = []
    cursor = 0.0
    for ws, we, _marks in merged:
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
        text = f"{chapter.number:02d}{_CAPTION_NUMBER_SEP}{chapter.label}"
        for seg in segments:
            lo = max(chapter.src_start, seg.start)
            hi = min(chapter.src_end, seg.end)
            if hi <= lo:
                continue
            local_start = (lo - seg.start) / seg.speed
            local_end = (hi - seg.start) / seg.speed
            seg.captions.append(Caption(text=text, start=local_start, end=local_end))


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
_CAPTION_FONTCOLOR = "white"
_CAPTION_BOXCOLOR = "black@0.62"  # semi-transparent backing box for legibility
_CAPTION_BOXBORDERW = 22  # generous padding so it reads as a designed tag
_CAPTION_NUMBER_SEP = " · "  # between the zero-padded chapter number and the label


def _drawtext_filter(caption: Caption, fontfile: str | None) -> str:
    # expansion=none keeps labels literal (no %{...} expansion / "Stray %").
    parts = [f"text={_escape_drawtext(caption.text)}", "expansion=none"]
    if fontfile:
        parts.append(f"fontfile={_escape_fontfile(fontfile)}")
    parts += [
        f"fontcolor={_CAPTION_FONTCOLOR}",
        f"fontsize={_CAPTION_FONTSIZE}",
        "box=1",
        f"boxcolor={_CAPTION_BOXCOLOR}",
        f"boxborderw={_CAPTION_BOXBORDERW}",
        # Top-left chapter tag (was bottom-centre). Fixed-pixel margin.
        f"x={_CAPTION_MARGIN}",
        f"y={_CAPTION_MARGIN}",
        # Escape the commas so the filtergraph parser keeps them inside between().
        f"enable=between(t\\,{caption.start:.3f}\\,{caption.end:.3f})",
    ]
    return "drawtext=" + ":".join(parts)


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
) -> str:
    """Build the filtergraph string for the review clip (pure, testable).

    Input 0 is the capture video; input 1 is a silent ``anullsrc`` used for
    timelapsed (and audio-less) segments so every concatenated segment has a
    matching audio stream. Every audio segment ends with an explicit
    ``aformat`` (see ``_AUDIO_SEGMENT_FORMAT``) so ``concat`` never collapses the
    narration to the filler's 8-bit format.
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

    concat = "".join(concat_inputs) + f"concat=n={len(plan)}:v=1:a=1[outv][outa]"
    return ";".join(chains + [concat])


def build_ffmpeg_edit_command(
    input_path: Path | str,
    output_path: Path | str,
    filter_script_path: Path | str,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Build the ffmpeg argv that renders the review clip from a filterscript.

    The filtergraph is read from ``filter_script_path`` via
    ``-filter_complex_script`` rather than inline, which sidesteps command-line
    length limits and shell-escaping pain for long graphs.
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
        "veryfast",
        "-crf",
        "20",
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
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    out: Callable[[str], object] = print,
) -> Path:
    """Render ``review.mp4`` for ``session_dir`` and return its path."""
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
    duration = probe_duration(capture, ffprobe=ffprobe)
    if duration <= 0:
        raise EditError(f"{capture} has no readable duration.")
    has_audio = probe_has_audio(capture, ffprobe=ffprobe)

    events = read_events(session_dir)
    plan = build_segment_plan(events, duration, pre=pre, post=post, speed=speed)

    out(f"Editing {capture.name} ({duration:.1f}s, audio={'yes' if has_audio else 'no'}).")
    out(describe_plan(plan, speed=speed, marker_count=len(events)))
    if not events:
        out(
            f"No markers found — rendering a straight {speed:g}x timelapse of the "
            "whole video."
        )

    output = session_dir / OUTPUT_FILENAME
    fontfile = _resolve_font(font)
    filter_complex = build_filter_complex(plan, fontfile=fontfile, has_audio=has_audio)

    # Pass the (potentially long, escaping-heavy) graph via a temp filterscript
    # rather than inline, then clean it up — only review.mp4 is left behind.
    script_path: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix="benchcam-edit-", suffix=".ffscript")
        script_path = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(filter_complex)
        command = build_ffmpeg_edit_command(
            capture, output, script_path, ffmpeg=ffmpeg
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
    return output
