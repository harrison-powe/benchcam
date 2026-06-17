"""Line-based interactive marker loop for bench-side use.

This is the v0 "manual" input: a plain terminal prompt where the operator types
short commands to log markers and notes while working. It is intentionally
line-based (one command per line read from stdin). There is no raw keypress
handling, no hotkeys, no curses, and nothing platform-specific (e.g. msvcrt).

The loop logic lives here, separate from the CLI, so it can be tested by feeding
fake input lines and capturing output -- no real keyboard required.

Commands:
    m <label>              add a marker (empty note), source "keyboard"
    m <label> | <note>     add a marker with a note
    note <text>            append a timestamped line to notes.md
    status                 print the session status summary
    help                   list these commands
    q | quit | end         end the session and exit the loop
    (blank line)           ignored
"""

from __future__ import annotations

from typing import Callable

from .. import session as session_mod
from ..recorders.base import Recorder
from ..session import Session

PROMPT = "benchcam> "

MARKER_SOURCE = "keyboard"

HELP_TEXT = """\
Commands:
  m <label>            add a marker with an empty note
  m <label> | <note>   add a marker with a note
  note <text>          append a timestamped line to notes.md
  status               print the session status summary
  help                 show this help
  q | quit | end       end the session and exit
  (blank line)         ignored"""


def _handle_marker(session: Session, arg: str, emit: Callable[[str], None]) -> None:
    if not arg.strip():
        emit("usage: m <label> [| <note>]")
        return
    if "|" in arg:
        label, note = arg.split("|", 1)
        label = label.strip()
        note = note.strip()
    else:
        label = arg.strip()
        note = ""
    if not label:
        emit("usage: m <label> [| <note>]")
        return
    marker = session_mod.add_marker(
        session, label, source=MARKER_SOURCE, note=note
    )
    line = (
        f"Marker #{marker.marker_index} @ {marker.elapsed_seconds:.3f}s "
        f"[{marker.source}] {marker.label}"
    )
    if marker.note:
        line += f" -- {marker.note}"
    emit(line)


def _handle_note(session: Session, arg: str, emit: Callable[[str], None]) -> None:
    if not arg.strip():
        emit("usage: note <text>")
        return
    line = session_mod.append_note(session, arg.strip())
    emit(f"Noted: {line}")


def run_interactive_loop(
    session: Session,
    recorder: Recorder,
    *,
    read_line: Callable[[str], str] | None = None,
    emit: Callable[[str], None] | None = None,
) -> Session:
    """Run the line-based interactive loop until the session is ended.

    ``read_line`` is called with the prompt and must return the next input line
    (without a trailing newline) or raise ``EOFError`` when input is exhausted.
    ``emit`` is used for all output. Both are injectable so tests can drive the
    loop with fake input and capture output. Defaults are resolved at call time
    (not import time) so ``builtins.input`` can be monkeypatched in tests.
    """
    if read_line is None:
        read_line = input
    if emit is None:
        emit = print

    emit(f"Interactive session {session.session_id} (recorder: {session.recorder}).")
    emit("Type 'help' for commands, 'end' to finish.")

    while True:
        try:
            raw = read_line(PROMPT)
        except EOFError:
            emit("")
            _end_session(session, recorder, emit)
            return session

        line = raw.strip()
        if not line:
            continue

        parts = line.split(None, 1)
        command = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if command == "m":
            _handle_marker(session, arg, emit)
        elif command == "note":
            _handle_note(session, arg, emit)
        elif command == "status":
            emit(session_mod.status_summary(session))
        elif command == "help":
            emit(HELP_TEXT)
        elif command in ("q", "quit", "end"):
            _end_session(session, recorder, emit)
            return session
        else:
            emit(f"Unknown command: {command!r}. Type 'help' for commands.")


def _end_session(
    session: Session, recorder: Recorder, emit: Callable[[str], None]
) -> None:
    recorder.stop()
    if session.status != session_mod.STATUS_ENDED:
        session_mod.end_session(session)
    emit(f"Ended session {session.session_id}.")
    emit(f"  markers: {session.markers_file}")
