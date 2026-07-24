"""Transcribe spoken narration near each marker using Whisper.

The bench workflow: while working, the operator presses a button/key to drop a
marker AND says out loud what is happening ("chip lifted", "reflow start", ...).
That narration is captured in the session's audio track. ``benchcam transcribe``
runs Whisper locally on the capture's audio to get a timestamped transcript, then
attaches the words spoken *around* each marker's ``elapsed_seconds`` to that
marker's ``narration`` column.

This is the FIRST of a two-step labeling pipeline; it only captures the raw
narration. ``benchcam label`` then reads ``narration`` and summarizes it into the
terse ``label``. Keeping raw narration in its own column means the two steps never
clobber each other and each is independently re-runnable (re-transcribing never
touches a terse label; re-labeling never touches the raw narration).

Design notes:

- Whisper (``openai-whisper``) + torch are an OPTIONAL dependency, declared as the
  ``[transcribe]`` extra. The import is lazy (only inside :func:`transcribe_audio`)
  so the stdlib-only core install — and the Raspberry Pi, which should NEVER pull
  in torch — is unaffected. If Whisper is missing we raise a clear
  :class:`TranscribeError` telling the user how to install it, never a cryptic
  ``ImportError``.
- ONE Whisper pass per session: the full-session transcript is SHARED with
  ``benchcam autochapter`` through ``transcript.json`` in the session folder.
  Whichever command runs first pays the (expensive, nondeterministic) Whisper
  pass and caches it stamped with the model/language used; the other command
  reuses it instead of re-rolling the dice on the same audio. A cache produced
  with a different model/language is ignored and replaced. Recovery from a bad
  cached transcript: delete ``transcript.json`` and re-run.
- This is meant to run on the laptop (GPU/RAM). It is a STANDALONE command and is
  intentionally NOT wired into ``benchcam edit``.
- The transcript->marker join (:func:`narration_for_marker` / :func:`plan_narrations`)
  is pure and is the unit-tested part; Whisper itself is mocked in tests.
- Provenance is preserved: narration is only written to a marker whose
  ``narration`` is empty (unless ``--overwrite``), and the marker's ``source`` is
  tagged with ``+transcribed`` so you can always tell an auto-captured marker from
  a purely typed one without losing the original origin (e.g. ``manual`` ->
  ``manual+transcribed``). A terse ``label`` is never touched here.

Capture audio is decoded by Whisper via ffmpeg, so the capture video file is fed
to Whisper directly — no separate audio-extraction step.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .editor import find_capture, probe_has_audio
from .markers import MARKERS_FILENAME, read_markers, update_marker

#: "small" balances accuracy and speed well on a laptop GPU (e.g. an RTX 2000
#: Ada); "base" was too small and misdetected/hallucinated. Override with --model
#: (e.g. "medium" for more accuracy, "base"/"tiny" for a faster, rougher pass).
DEFAULT_MODEL = "small"
ENV_MODEL = "BENCHCAM_WHISPER_MODEL"
#: Whisper auto-detects language by default, which can misfire on short/quiet
#: narration (e.g. flag English as Norwegian and hallucinate). Pinning it to
#: English removes that failure mode; override with --language for other audio.
DEFAULT_LANGUAGE = "en"
DEFAULT_WINDOW = 5.0

#: Full-session transcript cache, SHARED with ``benchcam autochapter``: whichever
#: command runs first writes it (with the model/language it was produced with);
#: both reuse it, so a session's audio is transcribed exactly once. To force a
#: fresh pass (e.g. after a bad transcription), delete this file and re-run.
TRANSCRIPT_FILENAME = "transcript.json"

#: Appended to a marker's ``source`` when transcription fills its label.
TRANSCRIBED_TAG = "transcribed"

_INSTALL_HINT = (
    "Whisper is not installed. The 'transcribe' command runs OpenAI Whisper "
    "(openai-whisper + torch) locally to turn your spoken narration into marker "
    "labels. Install it on the laptop with: pip install 'benchcam[transcribe]' "
    "(or: pip install openai-whisper). This pulls in torch and is meant for the "
    "laptop, NOT the Raspberry Pi."
)
_FFMPEG_HINT = (
    "ffmpeg/ffprobe were not found on PATH. Whisper uses ffmpeg to decode the "
    "capture's audio. Install it — Windows: 'winget install Gyan.FFmpeg'; Linux: "
    "'sudo apt install ffmpeg'; macOS: 'brew install ffmpeg'."
)


class TranscribeError(RuntimeError):
    """Raised for problems transcribing audio or labeling markers."""


@dataclass(frozen=True)
class TranscriptSegment:
    """A timestamped chunk of transcript text (seconds from the audio start)."""

    start: float
    end: float
    text: str


# --------------------------------------------------------------------------- #
# Model name resolution
# --------------------------------------------------------------------------- #

def resolve_model(model: str | None) -> str:
    """Pick the Whisper model: explicit flag, else $BENCHCAM_WHISPER_MODEL, else base."""
    if model:
        return model
    return os.environ.get(ENV_MODEL) or DEFAULT_MODEL


# --------------------------------------------------------------------------- #
# Full-session transcript cache (transcript.json) — shared with autochapter
# --------------------------------------------------------------------------- #

def transcript_path(session_dir: Path | str) -> Path:
    return Path(session_dir) / TRANSCRIPT_FILENAME


def load_cached_transcript(
    session_dir: Path | str,
    *,
    model: str | None = None,
    language: str | None = None,
) -> list[TranscriptSegment] | None:
    """Load the cached transcript, or ``None`` if missing/unreadable/mismatched.

    When ``model``/``language`` are given, the cache must have been produced with
    exactly those settings (recorded by :func:`save_transcript`) or it is treated
    as a miss — a 'small' transcript is not a valid answer to a ``--model medium``
    run. Left as ``None`` (display uses, e.g. the chapters sheet), any readable
    cache loads.

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
    if model is not None and data.get("model") != model:
        return None
    if language is not None and data.get("language") != language:
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
# Whisper (lazy import so the core/Pi install never needs torch)
# --------------------------------------------------------------------------- #

def _import_whisper():
    """Import ``whisper`` lazily, mapping a missing dep to a clear error.

    Wrapped in its own function so tests can monkeypatch it without needing the
    real package installed.
    """
    try:
        import whisper  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise TranscribeError(_INSTALL_HINT) from exc
    return whisper


def transcribe_audio(
    capture: Path | str, model: str, *, language: str = DEFAULT_LANGUAGE
) -> list[TranscriptSegment]:
    """Run Whisper on ``capture`` and return its timestamped segments.

    Whisper loads the audio from the (video) file via ffmpeg, so the capture path
    is passed through directly. ``language`` is passed to Whisper to skip (often
    wrong) auto-detection; an empty/None value lets Whisper auto-detect.
    """
    whisper = _import_whisper()
    loaded = whisper.load_model(model)
    # condition_on_previous_text=False: feeding each window's output back in as
    # the next window's prompt is Whisper's documented repetition-loop mechanism
    # (one bad window seeds the rest; the long quiet stretches of bench audio are
    # the classic trigger). The trade is real but small: that conditioning is
    # also what carries proper-noun consistency across windows, so domain terms
    # (already shaky here — "coyote" for Kyogre) may be misheard slightly more
    # often. Accepted: a loop destroys a session's narration; a misheard noun
    # costs one title edit.
    result = loaded.transcribe(
        str(capture), language=language or None, condition_on_previous_text=False
    )
    segments: list[TranscriptSegment] = []
    for seg in result.get("segments", []) or []:
        try:
            start = float(seg["start"])
            end = float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(seg.get("text", "")).strip()
        if text:
            segments.append(TranscriptSegment(start, end, text))
    return segments


# --------------------------------------------------------------------------- #
# Transcript -> marker join (pure, unit-tested)
# --------------------------------------------------------------------------- #

def has_text(value: str | None) -> bool:
    """True if a field already holds real (non-whitespace) content worth keeping."""
    return bool((value or "").strip())


def narration_for_marker(
    elapsed: float, segments: list[TranscriptSegment], window: float
) -> str:
    """Join the transcript text spoken within ``window`` seconds of a marker.

    A segment is included if it overlaps the inclusive window
    ``[elapsed - window, elapsed + window]`` — i.e. narration that starts just
    before or ends just after the marker is still captured. Segments are emitted
    in chronological order.
    """
    lo = elapsed - window
    hi = elapsed + window
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    texts = [
        s.text.strip()
        for s in ordered
        if s.end >= lo and s.start <= hi and s.text.strip()
    ]
    return " ".join(texts).strip()


def _tag_source(source: str | None) -> str:
    """Append the ``+transcribed`` provenance tag to a marker's source, once."""
    base = (source or "").strip()
    tags = base.split("+") if base else []
    if TRANSCRIBED_TAG in tags:
        return base
    if not base:
        return TRANSCRIBED_TAG
    return f"{base}+{TRANSCRIBED_TAG}"


@dataclass(frozen=True)
class NarrationAssignment:
    """One marker that transcription will (re)fill with narration, plus its tag."""

    marker_index: int
    narration: str
    source: str


def plan_narrations(
    markers: list[dict],
    segments: list[TranscriptSegment],
    *,
    window: float = DEFAULT_WINDOW,
    overwrite: bool = False,
) -> list[NarrationAssignment]:
    """Decide which markers get which transcribed narration (no I/O).

    - Markers that already have narration are skipped unless ``overwrite``. The
      terse ``label`` is irrelevant here — narration and label are independent.
    - Markers with no transcript text in their window are left untouched (we never
      blank out narration or write an empty string).
    - The returned source tags preserve the original origin (e.g.
      ``manual`` -> ``manual+transcribed``).
    """
    assignments: list[NarrationAssignment] = []
    for row in markers:
        try:
            index = int(row["marker_index"])
            elapsed = float(row["elapsed_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        if has_text(row.get("narration")) and not overwrite:
            continue
        narration = narration_for_marker(elapsed, segments, window)
        if not narration:
            continue
        assignments.append(
            NarrationAssignment(index, narration, _tag_source(row.get("source")))
        )
    return assignments


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def run_transcribe(
    session_dir: Path | str,
    *,
    model: str | None = None,
    language: str = DEFAULT_LANGUAGE,
    window: float = DEFAULT_WINDOW,
    overwrite: bool = False,
    out: Callable[[str], object] = print,
) -> list[NarrationAssignment]:
    """Transcribe a session's audio into each marker's ``narration`` column.

    The full-session transcript is SHARED with ``benchcam autochapter`` via
    ``transcript.json``: a cache produced with the same Whisper model/language is
    reused (no second Whisper pass over the same audio), and a fresh pass saves
    its result there for the other command. Deleting ``transcript.json`` forces a
    fresh pass (the recovery path for a bad transcription) — ``--overwrite`` only
    controls replacing existing narration, never the cache.

    ``language`` pins Whisper's language (default English) to avoid misdetection;
    pass an empty string to let Whisper auto-detect. Returns the assignments that
    were written (empty if nothing changed).
    """
    session_dir = Path(session_dir)
    if window < 0:
        raise TranscribeError("--window must be >= 0 seconds.")

    model_name = resolve_model(model)

    # One Whisper pass per session: reuse the shared transcript.json when it was
    # produced with the same model/language. The cache-hit path never opens the
    # capture — narration is rebuildable even with the (huge) video file absent.
    segments = load_cached_transcript(session_dir, model=model_name, language=language)
    if segments is not None:
        out(
            f"Using cached full-session transcript ({len(segments)} segment(s)) "
            f"from {TRANSCRIPT_FILENAME}; delete that file to force a fresh pass."
        )

    if segments is None:
        capture = find_capture(session_dir)

        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise TranscribeError(_FFMPEG_HINT)

        if not probe_has_audio(capture):
            out(
                f"{capture} has no audio track — nothing to transcribe. (Record with a "
                "microphone, e.g. the Yeti, to use auto-labeling.)"
            )
            return []

    markers_file = session_dir / MARKERS_FILENAME
    rows = read_markers(markers_file)
    if not rows:
        out(f"No markers in {markers_file} — nothing to label.")
        return []

    if segments is None:
        out(
            f"Transcribing {capture} with Whisper model {model_name!r} "
            f"(language={language or 'auto'}) (this can take a while)..."
        )
        segments = transcribe_audio(capture, model_name, language=language)
        out(f"Got {len(segments)} transcript segment(s).")
        save_transcript(session_dir, segments, model=model_name, language=language)
        out(f"Cached full transcript to {TRANSCRIPT_FILENAME} (shared with autochapter).")

    assignments = plan_narrations(
        rows, segments, window=window, overwrite=overwrite
    )
    if not assignments:
        out(
            "No marker narration to fill (existing narration kept; use --overwrite "
            "to replace)."
        )
        return []

    for assignment in assignments:
        update_marker(
            markers_file,
            assignment.marker_index,
            {"narration": assignment.narration, "source": assignment.source},
        )
        out(f"  marker #{assignment.marker_index}: {assignment.narration!r}")

    out(
        f"Captured narration for {len(assignments)} marker(s). Run "
        "'benchcam label' to summarize it into terse labels."
    )
    return assignments
