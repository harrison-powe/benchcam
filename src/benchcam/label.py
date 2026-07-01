"""Summarize each marker's raw narration into a terse technical ``label``.

Second step of the two-step labeling pipeline. ``benchcam transcribe`` fills each
marker's ``narration`` column with the raw spoken words captured near it;
``benchcam label`` reads that narration and asks a small, fast Claude model to
distill it into a short, technical event label ("okay so now I'm gonna connect
the power to this guy" -> "Power Connected"). Those labels drive the on-screen
chapter tag in ``benchcam edit`` and, later, YouTube chapters — so the label
describes WHAT'S HAPPENING, not the operator's verbatim words.

Design notes (mirrors ``transcribe.py``):

- The Anthropic SDK (``anthropic``) is an OPTIONAL dependency, declared as the
  ``[label]`` extra. The import is lazy (only inside :func:`_import_anthropic`)
  so the stdlib-only core install — and the Raspberry Pi — never pull it in. A
  missing package raises a clear :class:`LabelError`, never a raw ``ImportError``.
- This runs on the laptop and calls the network; it is a STANDALONE command and
  is intentionally NOT wired into ``benchcam edit``.
- The API key is read from ``ANTHROPIC_API_KEY`` (the SDK reads it from the
  environment automatically); it is never hardcoded. A missing key raises a clear
  :class:`LabelError` before any network call.
- The narration->label decision (:func:`plan_targets`) and the output sanitizer
  (:func:`sanitize_label`) are pure and are the unit-tested parts; the model call
  (:func:`summarize_narration`) is mocked in tests.

Provenance: a label is only written to a marker whose ``label`` is empty (unless
``--overwrite``), so hand-typed labels are preserved; the marker's ``source`` is
tagged with ``+ai-labeled`` (e.g. ``gpio+transcribed`` -> ``gpio+transcribed+ai-labeled``)
so an AI label is always distinguishable from a typed one. The raw ``narration``
is only ever read here, never modified, so this step is freely re-runnable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .markers import MARKERS_FILENAME, read_markers, set_marker_label

#: Small, fast model — this is short summarization, not reasoning.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
ENV_MODEL = "BENCHCAM_LABEL_MODEL"
ENV_API_KEY = "ANTHROPIC_API_KEY"

#: Appended to a marker's ``source`` when an AI label is written.
AI_LABELED_TAG = "ai-labeled"

#: Ceiling on model output; a terse label is 2-4 words, so this is generous.
MAX_TOKENS = 24
#: Hard guard so a runaway response can never become a paragraph on screen.
MAX_LABEL_WORDS = 6

_INSTALL_HINT = (
    "The Anthropic SDK is not installed. The 'label' command uses the Claude API "
    "to summarize marker narration into terse labels. Install it on the laptop "
    "with: pip install 'benchcam[label]' (or: pip install anthropic). This is "
    "meant for the laptop, NOT the Raspberry Pi."
)
_API_KEY_HINT = (
    f"No {ENV_API_KEY} set. The 'label' command calls the Claude API; set your "
    f"API key in the environment, e.g. (PowerShell) $env:{ENV_API_KEY}='sk-ant-...' "
    f"or (bash) export {ENV_API_KEY}=sk-ant-... — the key is never stored by benchcam."
)

SYSTEM_PROMPT = """\
You convert a bench engineer's spoken narration into a terse technical event
label for a build log and video chapter list.

Rules:
- Output ONLY the label. No quotes, no trailing punctuation, no preamble, no
  explanation.
- 2-4 words. Noun phrase or verb phrase, build-log register.
- Describe WHAT IS HAPPENING (the action or event), not the speaker's words.
  The narration is rambling, first-person, filler-heavy source material;
  distill the underlying event.
- Be technical and specific: prefer concrete component and action names.
- Title Case. Drop filler ("okay", "so", "gonna", "I think", "let me").

Examples:
narration: okay so now I'm gonna connect the power to this guy
label: Power Connected
narration: let me check that moteus came up enabled
label: Verifying Moteus Enable
narration: uh the chip just lifted off the board when I pushed on it
label: Chip Lifted
narration: alright I'm starting the reflow now
label: Reflow Start
narration: yeah so I'm bumping the current limit up to two amps and we'll see if it holds
label: Current Limit Raised
narration: okay first motion, it's actually spinning now
label: First Motion
narration: hmm that's not right, it's vibrating and making a nasty noise
label: Abnormal Vibration
narration: let me measure the resistance across these two pins real quick
label: Measuring Pin Resistance
narration: so this is the moteus dev kit r4 point eleven, this is what we're driving
label: Moteus R4.11 Overview
narration: and now I'm flashing the firmware onto the controller
label: Flashing Firmware
narration: it faulted out, threw an overvoltage error
label: Overvoltage Fault
"""


class LabelError(RuntimeError):
    """Raised for problems summarizing narration or labeling markers."""


# --------------------------------------------------------------------------- #
# Model name resolution
# --------------------------------------------------------------------------- #

def resolve_model(model: str | None) -> str:
    """Pick the model: explicit flag, else $BENCHCAM_LABEL_MODEL, else the default."""
    if model:
        return model
    return os.environ.get(ENV_MODEL) or DEFAULT_MODEL


# --------------------------------------------------------------------------- #
# Output sanitizer (pure, unit-tested)
# --------------------------------------------------------------------------- #

def sanitize_label(text: str | None) -> str:
    """Clean a model response into a single terse label line.

    - Keeps only the first non-empty line (a well-behaved model returns one line;
      this guards against a stray second line).
    - Strips surrounding quotes and collapses internal whitespace.
    - Strips a TRAILING period (and other trailing sentence punctuation), but
      preserves INTERNAL periods so version numbers like ``R4.11`` survive intact
      (never truncated to ``R4``).
    - Caps the word count as a hard guard against a runaway response.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    first = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    # Strip a symmetric surrounding quote if the model wrapped the label.
    for quote in ('"', "'"):
        if len(first) >= 2 and first.startswith(quote) and first.endswith(quote):
            first = first[1:-1].strip()
            break
    first = " ".join(first.split())  # collapse internal whitespace
    # Only trailing sentence punctuation is removed; internal '.' is preserved so
    # "R4.11" stays "R4.11" while "Power Connected." becomes "Power Connected".
    first = first.rstrip(" .,;:!")
    words = first.split()
    if len(words) > MAX_LABEL_WORDS:
        words = words[:MAX_LABEL_WORDS]
    return " ".join(words)


def build_user_message(narration: str) -> str:
    """The per-marker user turn: the narration, prompting for the label line."""
    return f"narration: {narration.strip()}\nlabel:"


# --------------------------------------------------------------------------- #
# Which markers get labeled (pure, unit-tested)
# --------------------------------------------------------------------------- #

def _tag_source(source: str | None) -> str:
    """Append the ``+ai-labeled`` provenance tag to a marker's source, once."""
    base = (source or "").strip()
    tags = base.split("+") if base else []
    if AI_LABELED_TAG in tags:
        return base
    if not base:
        return AI_LABELED_TAG
    return f"{base}+{AI_LABELED_TAG}"


@dataclass(frozen=True)
class LabelTarget:
    """One marker to summarize: its narration source text and new source tag."""

    marker_index: int
    narration: str
    source: str


def plan_targets(
    markers: list[dict], *, overwrite: bool = False
) -> list[LabelTarget]:
    """Decide which markers to label from their narration (no I/O, no API).

    - Only markers that HAVE narration are candidates (nothing to summarize
      otherwise) — narration-only, with no fallback to the ``label`` column.
    - Markers that already have a non-empty ``label`` are skipped unless
      ``overwrite`` (this preserves hand-typed labels).
    - Returned source tags preserve the original origin
      (e.g. ``gpio+transcribed`` -> ``gpio+transcribed+ai-labeled``).
    """
    targets: list[LabelTarget] = []
    for row in markers:
        try:
            index = int(row["marker_index"])
        except (KeyError, TypeError, ValueError):
            continue
        narration = (row.get("narration") or "").strip()
        if not narration:
            continue
        if (row.get("label") or "").strip() and not overwrite:
            continue
        targets.append(LabelTarget(index, narration, _tag_source(row.get("source"))))
    return targets


# --------------------------------------------------------------------------- #
# Anthropic client + call (lazy import; mocked in tests)
# --------------------------------------------------------------------------- #

def _import_anthropic():
    """Import ``anthropic`` lazily, mapping a missing dep to a clear error."""
    try:
        import anthropic  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise LabelError(_INSTALL_HINT) from exc
    return anthropic


def make_client():
    """Build an Anthropic client, failing clearly if the API key is unset.

    The key is checked BEFORE importing the SDK so a missing key gives the right
    error even on a core install without the ``[label]`` extra.
    """
    if not os.environ.get(ENV_API_KEY):
        raise LabelError(_API_KEY_HINT)
    anthropic = _import_anthropic()
    return anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment


def summarize_narration(client, narration: str, model: str) -> str:
    """Ask the model for a terse label for one marker's narration (raw text).

    Returns the model's raw text; callers run :func:`sanitize_label` on it.
    """
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_message(narration)}],
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

@dataclass(frozen=True)
class LabelResult:
    """One marker that was (or would be, in --dry-run) labeled."""

    marker_index: int
    label: str
    source: str


def run_label(
    session_dir: Path | str,
    *,
    model: str | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
    out: Callable[[str], object] = print,
) -> list[LabelResult]:
    """Summarize each marker's narration into its ``label`` via the Claude API.

    Returns the results produced (empty if nothing changed). With ``dry_run`` the
    API is still called (so you can review real output) but nothing is written.
    """
    session_dir = Path(session_dir)
    markers_file = session_dir / MARKERS_FILENAME
    rows = read_markers(markers_file)
    if not rows:
        out(f"No markers in {markers_file} — nothing to label.")
        return []

    targets = plan_targets(rows, overwrite=overwrite)
    if not targets:
        out(
            "No markers to label. Markers need narration first (run "
            "'benchcam transcribe'); existing labels are kept unless --overwrite."
        )
        return []

    model_name = resolve_model(model)
    client = make_client()  # validates ANTHROPIC_API_KEY + SDK before any work
    out(
        f"Summarizing narration for {len(targets)} marker(s) with {model_name!r}"
        + (" (dry run — nothing will be written)." if dry_run else ".")
    )

    results: list[LabelResult] = []
    for target in targets:
        raw = summarize_narration(client, target.narration, model_name)
        label = sanitize_label(raw)
        if not label:
            out(f"  marker #{target.marker_index}: (no label produced, skipped)")
            continue
        results.append(LabelResult(target.marker_index, label, target.source))
        arrow = "would set" if dry_run else "set"
        out(f"  marker #{target.marker_index}: {arrow} {label!r}")
        if not dry_run:
            set_marker_label(
                markers_file, target.marker_index, label, source=target.source
            )

    if dry_run:
        out(f"Dry run: {len(results)} label(s) proposed, none written.")
    else:
        out(f"Labeled {len(results)} marker(s) from narration.")
    return results
