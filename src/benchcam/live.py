"""Interactive live session shell for fast, single-keypress marker logging.

``benchcam live`` opens one long-running foreground process that holds the
active session in memory so marking is a single keystroke instead of a fresh
process that reloads the pointer, parses ``session.json``, and re-reads all of
``markers.csv`` to compute the next index.

Design:
- The session is loaded once by the caller; the next marker index is read once
  on entry (so it stays continuous with any markers ``benchcam mark`` already
  appended) and then incremented in memory.
- Every mark still APPENDS to ``markers.csv`` immediately via the existing
  :func:`benchcam.markers.append_marker` path, so a session that dies mid-bench
  keeps its markers. Only the index is kept in memory; the file is not re-read.
- Keypress reading is injected (see :mod:`benchcam.keypress`) so tests can drive
  the loop without a real TTY.

This module is purely additive: it does not change the schema, the recorders,
or the behavior of the existing commands.
"""

from __future__ import annotations

from typing import Callable

from . import clock
from . import session as session_mod
from .markers import Marker, append_marker, next_marker_index
from .recorders.base import Recorder
from .session import STATUS_CREATED, STATUS_ENDED, Session, SessionError

LEGEND = (
    "BenchCam live — fast marking. Keys:\n"
    "  [space]/[enter]  mark now (no label)\n"
    "  l                mark now, then type a label\n"
    "  n                add a line to notes.md\n"
    "  s                status (elapsed + marker count)\n"
    "  q                quit (ends the session)"
)


def _make_marker(session: Session, index: int, label: str, *, source: str = "manual") -> Marker:
    """Build a marker using the in-memory ``index`` and the session baseline.

    Mirrors :func:`benchcam.session.add_marker` but takes the index as an
    argument instead of re-reading the file to compute it.
    """
    now = clock.now()
    baseline = clock.from_iso(session_mod._elapsed_baseline(session))
    elapsed = max((now - baseline).total_seconds(), 0.0)
    return Marker(
        marker_index=index,
        elapsed_seconds=elapsed,
        wall_time=clock.to_iso(now),
        source=source,
        label=label,
    )


def _append_note(session: Session, text: str) -> None:
    """Append a single line to the session's ``notes.md``."""
    line = text.rstrip("\n")
    with session.notes_file.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _current_elapsed(session: Session) -> float:
    baseline = clock.from_iso(session_mod._elapsed_baseline(session))
    return max((clock.now() - baseline).total_seconds(), 0.0)


def _duration(session: Session) -> float:
    start = clock.from_iso(session_mod._elapsed_baseline(session))
    end = clock.from_iso(session.ended_wall_time) if session.ended_wall_time else clock.now()
    return max((end - start).total_seconds(), 0.0)


def live_session(
    session: Session,
    *,
    recorder: Recorder,
    read_key: Callable[[], str],
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], object] = print,
) -> int:
    """Run the interactive marking loop for ``session`` until the user quits.

    The caller resolves the active session and recorder; this function handles
    the status checks, starts the session if needed (same effect as ``run``),
    and then loops on injected keypresses.
    """
    if session.status == STATUS_ENDED:
        raise SessionError(
            f"Session {session.session_id} has already ended. "
            "Create a new one with 'benchcam new' first."
        )
    if session.status == STATUS_CREATED:
        recorder.start(session.folder)
        session_mod.start_session(session)

    out(LEGEND)
    out(f"Live on session {session.session_id} (recorder: {session.recorder}).")

    # Read the index once; from here it is tracked in memory.
    next_index = next_marker_index(session.markers_file)

    while True:
        try:
            key = read_key()
        except KeyboardInterrupt:
            key = "q"

        if key == "":  # EOF / no more input -> clean quit
            key = "q"

        if key in (" ", "\r", "\n"):
            marker = _make_marker(session, next_index, "")
            append_marker(session.markers_file, marker)
            next_index += 1
            out(f"  marker #{marker.marker_index} @ {marker.elapsed_seconds:.3f}s")
            continue

        command = key.lower()

        if command == "l":
            label = input_fn("label> ").strip()
            marker = _make_marker(session, next_index, label)
            append_marker(session.markers_file, marker)
            next_index += 1
            shown = f" {marker.label}" if marker.label else " (no label)"
            out(f"  marker #{marker.marker_index} @ {marker.elapsed_seconds:.3f}s{shown}")
        elif command == "n":
            text = input_fn("note> ")
            _append_note(session, text)
            out("  note appended to notes.md")
        elif command == "s":
            count = next_index - 1
            out(
                f"  session {session.session_id} | elapsed {_current_elapsed(session):.3f}s "
                f"| markers {count}"
            )
        elif command == "q":
            recorder.stop()
            session_mod.end_session(session)
            count = next_index - 1
            out(
                f"Ended session {session.session_id}: {count} marker(s), "
                f"{_duration(session):.3f}s."
            )
            out(f"  folder:  {session.storage_path}")
            out(f"  markers: {session.markers_file}")
            return 0
        # Unknown keys are ignored so a stray keystroke never marks or exits.
