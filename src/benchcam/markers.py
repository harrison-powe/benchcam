"""Marker logging.

Markers are time-stamped events written to ``markers.csv`` inside a session
folder. A marker can be created by the operator (``benchcam mark "label"``) or,
later, by an external source feeding events to BenchCam. Either way, BenchCam
only records the event; it never acts on hardware.

CSV columns:
- marker_index    monotonically increasing index within the session (starts at 1)
- elapsed_seconds seconds since the session started recording (run time)
- wall_time       ISO 8601 local timestamp of the marker
- source          where the marker came from (e.g. "manual", "external")
- label           free-text description
- note            optional free-text note (empty string when omitted)
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

MARKERS_FILENAME = "markers.csv"

FIELDNAMES = [
    "marker_index",
    "elapsed_seconds",
    "wall_time",
    "source",
    "label",
    "note",
]


@dataclass
class Marker:
    marker_index: int
    elapsed_seconds: float
    wall_time: str
    source: str
    label: str
    note: str = ""

    def as_row(self) -> dict:
        return {
            "marker_index": self.marker_index,
            "elapsed_seconds": f"{self.elapsed_seconds:.3f}",
            "wall_time": self.wall_time,
            "source": self.source,
            "label": self.label,
            "note": self.note,
        }


def init_markers_file(path: Path) -> None:
    """Create ``markers.csv`` with just the header row."""
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()


def read_markers(path: Path) -> list[dict]:
    """Read all marker rows (excluding the header) as dicts."""
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def next_marker_index(path: Path) -> int:
    """Return the next 1-based marker index for the file."""
    return len(read_markers(path)) + 1


def append_marker(path: Path, marker: Marker) -> None:
    """Append a single marker row to ``markers.csv``."""
    path = Path(path)
    file_exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow(marker.as_row())
