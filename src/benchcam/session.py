"""Session model and on-disk layout.

A session is a folder under the sessions root:

    sessions/YYYY-MM-DD_HH-MM-SS/
        session.json   metadata about the session
        markers.csv    time-stamped markers
        notes.md       free-form operator notes

The "active" session (the one ``run``/``mark``/``end`` operate on) is tracked by
a small pointer file at ``sessions/.active`` that holds the session folder name.

Everything here is local files only. No cloud sync.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import clock
from .markers import (
    Marker,
    append_marker,
    init_markers_file,
    next_marker_index,
    read_markers,
)

DEFAULT_SESSIONS_ROOT = Path("sessions")
ACTIVE_POINTER_NAME = ".active"

SESSION_FILENAME = "session.json"
MARKERS_FILENAME = "markers.csv"
NOTES_FILENAME = "notes.md"

STATUS_CREATED = "created"
STATUS_RUNNING = "running"
STATUS_ENDED = "ended"


class SessionError(RuntimeError):
    """Raised for session lifecycle / state problems."""


@dataclass
class Session:
    """In-memory view of a session's metadata (mirrors session.json)."""

    session_id: str
    created_wall_time: str
    profile: str
    camera: str
    microphone: str
    recorder: str
    storage_path: str
    notes: str = ""
    status: str = STATUS_CREATED
    started_wall_time: str | None = None
    ended_wall_time: str | None = None

    @property
    def folder(self) -> Path:
        return Path(self.storage_path)

    @property
    def session_file(self) -> Path:
        return self.folder / SESSION_FILENAME

    @property
    def markers_file(self) -> Path:
        return self.folder / MARKERS_FILENAME

    @property
    def notes_file(self) -> Path:
        return self.folder / NOTES_FILENAME

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        """Write session.json atomically-ish (write temp then replace)."""
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.session_file.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.session_file)


def _unique_folder(root: Path, name: str) -> Path:
    """Return a non-existing folder path, adding a numeric suffix if needed."""
    candidate = root / name
    counter = 2
    while candidate.exists():
        candidate = root / f"{name}_{counter}"
        counter += 1
    return candidate


def create_session(
    root: Path = DEFAULT_SESSIONS_ROOT,
    *,
    profile: str = "default",
    camera: str = "",
    microphone: str = "",
    recorder: str = "null",
    notes: str = "",
    set_active: bool = True,
) -> Session:
    """Create a new session folder with its three files and return the Session."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    created = clock.now()
    name = clock.folder_timestamp(created)
    folder = _unique_folder(root, name)
    folder.mkdir(parents=True, exist_ok=False)

    session = Session(
        session_id=folder.name,
        created_wall_time=clock.to_iso(created),
        profile=profile,
        camera=camera,
        microphone=microphone,
        recorder=recorder,
        storage_path=str(folder),
        notes=notes,
        status=STATUS_CREATED,
    )
    session.save()

    init_markers_file(session.markers_file)
    session.notes_file.write_text(
        f"# Notes for session {session.session_id}\n\n", encoding="utf-8"
    )

    if set_active:
        set_active_session(root, folder.name)

    return session


def load_session(folder: Path) -> Session:
    """Load a Session from its folder."""
    folder = Path(folder)
    session_file = folder / SESSION_FILENAME
    if not session_file.exists():
        raise SessionError(f"No session.json found in {folder}")
    data = json.loads(session_file.read_text(encoding="utf-8"))
    return Session.from_dict(data)


def _active_pointer(root: Path) -> Path:
    return Path(root) / ACTIVE_POINTER_NAME


def set_active_session(root: Path, folder_name: str) -> None:
    """Record which session is active by writing the pointer file."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    _active_pointer(root).write_text(folder_name + "\n", encoding="utf-8")


def active_session_name(root: Path = DEFAULT_SESSIONS_ROOT) -> str | None:
    """Return the active session folder name, or None if there is none."""
    pointer = _active_pointer(Path(root))
    if not pointer.exists():
        return None
    name = pointer.read_text(encoding="utf-8").strip()
    return name or None


def clear_active_session(root: Path = DEFAULT_SESSIONS_ROOT) -> None:
    """Remove the active-session pointer if it exists."""
    pointer = _active_pointer(Path(root))
    if pointer.exists():
        pointer.unlink()


def get_active_session(root: Path = DEFAULT_SESSIONS_ROOT) -> Session:
    """Load the active session, or raise SessionError if there is none."""
    root = Path(root)
    pointer = _active_pointer(root)
    if not pointer.exists():
        raise SessionError(
            "No active session. Create one with 'benchcam new' first."
        )
    folder_name = pointer.read_text(encoding="utf-8").strip()
    folder = root / folder_name
    if not folder.exists():
        raise SessionError(
            f"Active session points to {folder}, but that folder is missing."
        )
    return load_session(folder)


def start_session(session: Session) -> Session:
    """Mark a session as running and stamp its start time."""
    if session.status == STATUS_RUNNING:
        raise SessionError(f"Session {session.session_id} is already running.")
    if session.status == STATUS_ENDED:
        raise SessionError(f"Session {session.session_id} has already ended.")
    session.status = STATUS_RUNNING
    session.started_wall_time = clock.to_iso(clock.now())
    session.save()
    return session


def end_session(session: Session) -> Session:
    """Mark a session as ended and stamp its end time.

    Also clears the active-session pointer when the ended session is the
    active one, so a fresh ``benchcam status`` does not point at a closed
    session.
    """
    if session.status == STATUS_ENDED:
        raise SessionError(f"Session {session.session_id} has already ended.")
    session.status = STATUS_ENDED
    session.ended_wall_time = clock.to_iso(clock.now())
    session.save()

    root = session.folder.parent
    if active_session_name(root) == session.folder.name:
        clear_active_session(root)
    return session


def _elapsed_baseline(session: Session) -> str:
    """Pick the reference time for elapsed_seconds (run start, else creation)."""
    return session.started_wall_time or session.created_wall_time


def add_marker(
    session: Session,
    label: str,
    *,
    source: str = "manual",
    note: str = "",
) -> Marker:
    """Append a marker to the session and return it."""
    now = clock.now()
    baseline = clock.from_iso(_elapsed_baseline(session))
    elapsed = (now - baseline).total_seconds()
    index = next_marker_index(session.markers_file)
    marker = Marker(
        marker_index=index,
        elapsed_seconds=max(elapsed, 0.0),
        wall_time=clock.to_iso(now),
        source=source,
        label=label,
        note=note,
    )
    append_marker(session.markers_file, marker)
    return marker


def marker_count(session: Session) -> int:
    """Return the number of markers logged for the session."""
    return len(read_markers(session.markers_file))
