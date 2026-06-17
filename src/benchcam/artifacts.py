"""Manual media/artifact attachment.

BenchCam does not capture video in v0. Instead, you record manually (OBS, a
webcam app, a phone, an audio recorder, ...) and then *attach* the resulting
file to a session. Attaching either copies the file into the session's
``media/`` folder (default) or just records a reference to wherever it already
lives (for very large files).

Each attachment is recorded as a row in ``artifacts.csv`` inside the session
folder. This module is plain standard library (``pathlib`` + ``shutil`` + ``csv``)
and never touches hardware.

artifacts.csv columns:
    artifact_index  added_wall_time  kind  label  original_path
    stored_path  size_bytes  mode
"""

from __future__ import annotations

import csv
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import clock

if TYPE_CHECKING:  # avoid an import cycle; Session is only used for typing
    from .session import Session

ARTIFACTS_FIELDNAMES = [
    "artifact_index",
    "added_wall_time",
    "kind",
    "label",
    "original_path",
    "stored_path",
    "size_bytes",
    "mode",
]

KINDS = ("video", "audio", "image", "other")

MODE_COPY = "copy"
MODE_REFERENCE = "reference"
MODES = (MODE_COPY, MODE_REFERENCE)

_EXTENSION_KINDS = {
    "video": {".mp4", ".mov", ".mkv", ".avi", ".webm"},
    "audio": {".wav", ".mp3", ".m4a", ".flac"},
    "image": {".jpg", ".jpeg", ".png", ".webp"},
}


class ArtifactError(RuntimeError):
    """Raised for media-attachment problems caused by user input."""


@dataclass
class Artifact:
    artifact_index: int
    added_wall_time: str
    kind: str
    label: str
    original_path: str
    stored_path: str
    size_bytes: int
    mode: str

    def as_row(self) -> dict:
        return {
            "artifact_index": self.artifact_index,
            "added_wall_time": self.added_wall_time,
            "kind": self.kind,
            "label": self.label,
            "original_path": self.original_path,
            "stored_path": self.stored_path,
            "size_bytes": self.size_bytes,
            "mode": self.mode,
        }


def infer_kind(path: Path) -> str:
    """Infer an artifact kind from a file extension (case-insensitive)."""
    ext = Path(path).suffix.lower()
    for kind, extensions in _EXTENSION_KINDS.items():
        if ext in extensions:
            return kind
    return "other"


def validate_kind(kind: str) -> str:
    """Return the kind if valid, else raise ArtifactError."""
    if kind not in KINDS:
        valid = ", ".join(KINDS)
        raise ArtifactError(f"Invalid kind {kind!r}. Choose one of: {valid}.")
    return kind


def init_artifacts_file(path: Path) -> None:
    """Create ``artifacts.csv`` with just the header row."""
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=ARTIFACTS_FIELDNAMES)
        writer.writeheader()


def read_artifacts(path: Path) -> list[dict]:
    """Read all artifact rows (excluding the header) as dicts."""
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def next_artifact_index(path: Path) -> int:
    """Return the next 1-based artifact index for the file."""
    return len(read_artifacts(path)) + 1


def append_artifact(path: Path, artifact: Artifact) -> None:
    """Append a single artifact row to ``artifacts.csv``."""
    path = Path(path)
    file_exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=ARTIFACTS_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow(artifact.as_row())


def _unique_destination(media_dir: Path, filename: str) -> Path:
    """Return a non-existing path in media_dir, adding -1, -2, ... if needed."""
    candidate = media_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while True:
        candidate = media_dir / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def ensure_session_media(session: "Session") -> None:
    """Lazily create ``media/`` and ``artifacts.csv`` for older sessions."""
    session.media_dir.mkdir(parents=True, exist_ok=True)
    if not session.artifacts_file.exists():
        init_artifacts_file(session.artifacts_file)


def attach_media(
    session: "Session",
    source: Path,
    *,
    label: str = "",
    kind: str | None = None,
    mode: str = MODE_COPY,
) -> Artifact:
    """Attach a media file to a session, returning the recorded Artifact.

    In copy mode the file is copied into ``<session>/media/`` (never
    overwriting an existing file). In reference mode the file is left in place
    and only the manifest row is written.
    """
    source = Path(source)
    if mode not in MODES:
        valid = ", ".join(MODES)
        raise ArtifactError(f"Invalid mode {mode!r}. Choose one of: {valid}.")
    if not source.exists():
        raise ArtifactError(f"Source file does not exist: {source}")
    if source.is_dir():
        raise ArtifactError(f"Source is a directory, not a file: {source}")

    resolved_source = source.resolve()
    size_bytes = resolved_source.stat().st_size
    resolved_kind = validate_kind(kind) if kind else infer_kind(resolved_source)

    ensure_session_media(session)

    if mode == MODE_COPY:
        destination = _unique_destination(session.media_dir, source.name)
        shutil.copy2(resolved_source, destination)
        stored_path = destination.relative_to(session.folder).as_posix()
    else:  # reference
        stored_path = ""

    artifact = Artifact(
        artifact_index=next_artifact_index(session.artifacts_file),
        added_wall_time=clock.to_iso(clock.now()),
        kind=resolved_kind,
        label=label,
        original_path=str(resolved_source),
        stored_path=stored_path,
        size_bytes=size_bytes,
        mode=mode,
    )
    append_artifact(session.artifacts_file, artifact)
    return artifact
