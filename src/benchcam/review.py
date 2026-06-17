"""Generate a human-readable Markdown review of a session.

``benchcam review`` turns the local session source artifacts (``session.json``,
``markers.csv``, ``artifacts.csv``, ``notes.md``) into a single ``review.md``
suitable for build logs, debugging notes, and proof-of-work packaging.

This is read-only over the source artifacts: it never modifies them, and it does
not touch, copy, or process any media files. Output is deterministic -- it does
not embed the time the review was generated -- so it is easy to test and diff.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .artifacts import read_artifacts
from .markers import read_markers

if TYPE_CHECKING:  # only needed for typing; avoids any import cycle
    from .session import Session

REVIEW_FILENAME = "review.md"

CHECKLIST = [
    "Identify key moments worth clipping or referencing.",
    "Confirm markers line up with attached media.",
    "Pull useful observations into the build log.",
    "Decide whether any follow-up test is needed.",
]

_MARKER_COLUMNS = ["Index", "Elapsed seconds", "Wall time", "Source", "Label", "Note"]
_ARTIFACT_COLUMNS = [
    "Index",
    "Kind",
    "Label",
    "Mode",
    "Stored path",
    "Original path",
    "Size bytes",
]


class ReviewError(RuntimeError):
    """Raised for review-generation problems caused by user input."""


def _escape_cell(value: str) -> str:
    """Make a value safe to put inside a Markdown table cell.

    Escapes pipe characters so labels/notes containing ``|`` do not break the
    table, and flattens any newlines to spaces.
    """
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", " ")
    text = text.replace("|", "\\|")
    return text


def _table(columns: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_escape_cell(c) for c in row) + " |")
    return lines


def _session_section(session: "Session") -> list[str]:
    lines = [
        "## Session",
        "",
        f"- Session path: {session.storage_path}",
        f"- Session ID: {session.session_id}",
        f"- Status: {session.status}",
        f"- Profile: {session.profile}",
        f"- Recorder: {session.recorder}",
        f"- Camera: {session.camera}",
        f"- Microphone: {session.microphone}",
        f"- Created wall time: {session.created_wall_time}",
    ]
    if session.started_wall_time:
        lines.append(f"- Started wall time: {session.started_wall_time}")
    if session.ended_wall_time:
        lines.append(f"- Ended wall time: {session.ended_wall_time}")
    return lines


def _markers_section(markers: list[dict]) -> list[str]:
    lines = ["## Markers", ""]
    if not markers:
        lines.append("No markers recorded.")
        return lines
    rows = [
        [
            m.get("marker_index", ""),
            m.get("elapsed_seconds", ""),
            m.get("wall_time", ""),
            m.get("source", ""),
            m.get("label", ""),
            m.get("note", ""),
        ]
        for m in markers
    ]
    lines.extend(_table(_MARKER_COLUMNS, rows))
    return lines


def _artifacts_section(artifacts: list[dict]) -> list[str]:
    lines = ["## Artifacts", ""]
    if not artifacts:
        lines.append("No artifacts attached.")
        return lines
    rows = [
        [
            a.get("artifact_index", ""),
            a.get("kind", ""),
            a.get("label", ""),
            a.get("mode", ""),
            a.get("stored_path", ""),
            a.get("original_path", ""),
            a.get("size_bytes", ""),
        ]
        for a in artifacts
    ]
    lines.extend(_table(_ARTIFACT_COLUMNS, rows))
    return lines


def _notes_section(notes_text: str) -> list[str]:
    lines = ["## Notes", ""]
    if not notes_text or not notes_text.strip():
        lines.append("No notes recorded.")
        return lines
    lines.append(notes_text.rstrip("\n"))
    return lines


def _checklist_section() -> list[str]:
    lines = ["## Review Checklist", ""]
    lines.extend(f"- [ ] {item}" for item in CHECKLIST)
    return lines


def build_review(session: "Session") -> str:
    """Build the review Markdown text for a session (no files are written)."""
    markers = read_markers(session.markers_file)
    artifacts = read_artifacts(session.artifacts_file)
    notes_text = (
        session.notes_file.read_text(encoding="utf-8")
        if session.notes_file.exists()
        else ""
    )

    blocks = [
        ["# BenchCam Session Review"],
        _session_section(session),
        _markers_section(markers),
        _artifacts_section(artifacts),
        _notes_section(notes_text),
        _checklist_section(),
    ]
    # Join blocks with a blank line between them; end with a trailing newline.
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"


def write_review(session: "Session", output: Path | None = None) -> Path:
    """Write the review to ``output`` (or ``<session>/review.md``) and return it."""
    text = build_review(session)
    if output is None:
        out_path = session.folder / REVIEW_FILENAME
    else:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return out_path
