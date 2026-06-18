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
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import clock
from .markers import (
    Marker,
    append_marker,
    init_markers_file,
    next_marker_index,
)

DEFAULT_SESSIONS_ROOT = Path("sessions")
ACTIVE_POINTER_NAME = ".active"

SESSION_FILENAME = "session.json"
MARKERS_FILENAME = "markers.csv"
NOTES_FILENAME = "notes.md"
# Sidecar written by the OBS recorder; repointed on folder rename if it points
# inside the session folder. (Kept here to avoid importing a recorder module.)
OBS_POINTER_FILENAME = "obs_recording.txt"

STATUS_CREATED = "created"
STATUS_RUNNING = "running"
STATUS_ENDED = "ended"


class SessionError(RuntimeError):
    """Raised for session lifecycle / state problems."""


def slugify(name: str) -> str:
    """Turn a friendly name into a filesystem-safe slug.

    Lowercases, turns whitespace into hyphens, drops anything that isn't a-z0-9
    or hyphen, collapses repeated hyphens, and trims leading/trailing hyphens.
    Returns "" when nothing usable remains (so the folder stays timestamp-only).
    """
    text = (name or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^a-z0-9-]", "", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


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
    name: str = ""
    status: str = STATUS_CREATED
    started_wall_time: str | None = None
    ended_wall_time: str | None = None

    @property
    def folder(self) -> Path:
        return Path(self.storage_path)

    @property
    def display_name(self) -> str:
        """Friendly name if set, else the folder name (old sessions)."""
        return self.name or self.session_id

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
    name: str = "",
    set_active: bool = True,
) -> Session:
    """Create a new session folder with its three files and return the Session.

    When ``name`` is given, the folder is ``<timestamp>_<slug>`` (timestamp kept
    for sorting/uniqueness); the original human-readable name is stored in
    session.json. With no name, the folder is timestamp-only as before.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    created = clock.now()
    timestamp = clock.folder_timestamp(created)
    slug = slugify(name)
    folder_name = f"{timestamp}_{slug}" if slug else timestamp
    folder = _unique_folder(root, folder_name)
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
        name=(name or "").strip(),
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


def _timestamp_prefix(session: Session, folder: Path) -> str:
    """The original ``YYYY-MM-DD_HH-MM-SS`` prefix to keep across a rename."""
    try:
        return clock.folder_timestamp(clock.from_iso(session.created_wall_time))
    except (TypeError, ValueError):
        # Fall back to the date_time portion of the existing folder name.
        parts = folder.name.split("_")
        return "_".join(parts[:2]) if len(parts) >= 2 else folder.name


def _fix_active_pointer(root: Path, old_name: str, new_name: str) -> None:
    pointer = _active_pointer(root)
    try:
        if pointer.exists() and pointer.read_text(encoding="utf-8").strip() == old_name:
            pointer.write_text(new_name + "\n", encoding="utf-8")
    except OSError:
        pass


def _repoint_obs_pointer(old_folder: Path, new_folder: Path) -> None:
    """If obs_recording.txt pointed inside the old folder, repoint it.

    External paths (OBS's own recording folder, when collect failed) are left
    untouched — the video really is over there.
    """
    pointer = Path(new_folder) / OBS_POINTER_FILENAME
    try:
        if not pointer.exists():
            return
        target = Path(pointer.read_text(encoding="utf-8").strip())
    except OSError:
        return
    try:
        rel = target.relative_to(old_folder)
    except ValueError:
        return  # points outside the session folder; leave it
    try:
        pointer.write_text(str(Path(new_folder) / rel) + "\n", encoding="utf-8")
    except OSError:
        pass


def rename_session(folder: Path, new_name: str) -> Session:
    """Rename an ended session's folder to ``<timestamp>_<new-slug>`` on disk.

    Keeps the original timestamp prefix (for sorting/uniqueness); only the slug
    changes. Updates session.json's name/session_id/storage_path and repoints the
    ``.active`` pointer and ``obs_recording.txt`` if they referenced the folder.
    Refuses a running session and handles target-name collisions by appending a
    numeric suffix. capture.*/markers.csv/notes.md/review.mp4 move with the
    folder. Returns the updated Session.
    """
    folder = Path(folder)
    session = load_session(folder)
    if session.status == STATUS_RUNNING:
        raise SessionError(
            f"Session {session.session_id} is still running; end it before renaming."
        )

    root = folder.parent
    timestamp = _timestamp_prefix(session, folder)
    slug = slugify(new_name)
    new_folder_name = f"{timestamp}_{slug}" if slug else timestamp
    target = root / new_folder_name

    if target != folder:
        if target.exists():
            target = _unique_folder(root, new_folder_name)
        folder.rename(target)
        _fix_active_pointer(root, folder.name, target.name)
        _repoint_obs_pointer(folder, target)

    session.name = (new_name or "").strip()
    session.storage_path = str(target)
    session.session_id = target.name
    session.save()
    return session


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
    """Mark a session as ended and stamp its end time."""
    if session.status == STATUS_ENDED:
        raise SessionError(f"Session {session.session_id} has already ended.")
    session.status = STATUS_ENDED
    session.ended_wall_time = clock.to_iso(clock.now())
    session.save()
    return session


def _elapsed_baseline(session: Session) -> str:
    """Pick the reference time for elapsed_seconds (run start, else creation)."""
    return session.started_wall_time or session.created_wall_time


def add_marker(session: Session, label: str, *, source: str = "manual") -> Marker:
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
    )
    append_marker(session.markers_file, marker)
    return marker
