"""Auto-label markers from spoken narration using Whisper.

The bench workflow: while working, the operator presses a button/key to drop a
marker AND says out loud what is happening ("chip lifted", "reflow start", ...).
That narration is captured in the session's audio track. ``benchcam transcribe``
runs Whisper locally on the capture's audio to get a timestamped transcript, then
attaches the words spoken *around* each marker's ``elapsed_seconds`` as that
marker's label.

Design notes:

- Whisper (``openai-whisper``) + torch are an OPTIONAL dependency, declared as the
  ``[transcribe]`` extra. The import is lazy (only inside :func:`transcribe_audio`)
  so the stdlib-only core install — and the Raspberry Pi, which should NEVER pull
  in torch — is unaffected. If Whisper is missing we raise a clear
  :class:`TranscribeError` telling the user how to install it, never a cryptic
  ``ImportError``.
- This is meant to run on the laptop (GPU/RAM). It is a STANDALONE command and is
  intentionally NOT wired into ``benchcam edit``.
- The transcript->marker join (:func:`label_for_marker` / :func:`plan_labels`) is
  pure and is the unit-tested part; Whisper itself is mocked in tests.
- Provenance is preserved: a transcribed label is only written to a marker whose
  label is empty (unless ``--overwrite``), and the marker's ``source`` is tagged
  with ``+transcribed`` so you can always tell an auto-label from a typed one
  without losing the original origin (e.g. ``manual`` -> ``manual+transcribed``).

Capture audio is decoded by Whisper via ffmpeg, so the capture video file is fed
to Whisper directly — no separate audio-extraction step.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .editor import find_capture, probe_has_audio
from .markers import MARKERS_FILENAME, read_markers, set_marker_label

DEFAULT_MODEL = "base"
ENV_MODEL = "BENCHCAM_WHISPER_MODEL"
DEFAULT_WINDOW = 5.0

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


def transcribe_audio(capture: Path | str, model: str) -> list[TranscriptSegment]:
    """Run Whisper on ``capture`` and return its timestamped segments.

    Whisper loads the audio from the (video) file via ffmpeg, so the capture path
    is passed through directly.
    """
    whisper = _import_whisper()
    loaded = whisper.load_model(model)
    result = loaded.transcribe(str(capture))
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

def is_meaningful_label(label: str | None) -> bool:
    """True if a marker already has a real, operator-set label worth keeping."""
    return bool((label or "").strip())


def label_for_marker(
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
class LabelAssignment:
    """One marker that transcription will (re)label, with its new source tag."""

    marker_index: int
    label: str
    source: str


def plan_labels(
    markers: list[dict],
    segments: list[TranscriptSegment],
    *,
    window: float = DEFAULT_WINDOW,
    overwrite: bool = False,
) -> list[LabelAssignment]:
    """Decide which markers get which transcribed labels (no I/O).

    - Markers that already have a meaningful label are skipped unless ``overwrite``.
    - Markers with no transcript text in their window are left untouched (we never
      blank out a label or write an empty one).
    - The returned source tags preserve the original origin (e.g.
      ``manual`` -> ``manual+transcribed``).
    """
    assignments: list[LabelAssignment] = []
    for row in markers:
        try:
            index = int(row["marker_index"])
            elapsed = float(row["elapsed_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        if is_meaningful_label(row.get("label")) and not overwrite:
            continue
        label = label_for_marker(elapsed, segments, window)
        if not label:
            continue
        assignments.append(
            LabelAssignment(index, label, _tag_source(row.get("source")))
        )
    return assignments


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def run_transcribe(
    session_dir: Path | str,
    *,
    model: str | None = None,
    window: float = DEFAULT_WINDOW,
    overwrite: bool = False,
    out: Callable[[str], object] = print,
) -> list[LabelAssignment]:
    """Transcribe a session's audio and auto-label its markers.

    Returns the assignments that were written (empty if nothing changed).
    """
    session_dir = Path(session_dir)
    if window < 0:
        raise TranscribeError("--window must be >= 0 seconds.")

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

    model_name = resolve_model(model)
    out(f"Transcribing {capture} with Whisper model {model_name!r} (this can take a while)...")
    segments = transcribe_audio(capture, model_name)
    out(f"Got {len(segments)} transcript segment(s).")

    assignments = plan_labels(
        rows, segments, window=window, overwrite=overwrite
    )
    if not assignments:
        out("No marker labels to fill (existing labels kept; use --overwrite to replace).")
        return []

    for assignment in assignments:
        set_marker_label(
            markers_file,
            assignment.marker_index,
            assignment.label,
            source=assignment.source,
        )
        out(f"  marker #{assignment.marker_index}: {assignment.label!r}")

    out(f"Labeled {len(assignments)} marker(s) from transcription.")
    return assignments
