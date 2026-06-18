"""Single-keypress reading, isolated behind one small function per OS.

The live session shell needs to read one key at a time without waiting for the
user to press Enter. That requires OS-specific terminal handling, so it is kept
here (and injectable) rather than scattered through the loop:

- Windows: ``msvcrt.getwch`` reads a single character with no echo.
- POSIX (Linux/macOS, incl. WSL): put the terminal in cbreak mode for one read
  via ``termios``/``tty``, then restore it.
- Fallback: if there is no real TTY (piped stdin, captured tests), read one
  character from ``sys.stdin``.

Stdlib only. The returned value is the character that was pressed; an empty
string ``""`` means end-of-input (EOF), which the caller treats as "quit".
"""

from __future__ import annotations

import os
import sys


def _read_key_windows() -> str:
    import msvcrt  # noqa: PLC0415 - Windows-only import

    ch = msvcrt.getwch()
    # Function/arrow keys arrive as a prefix byte followed by a code; swallow
    # the second byte so it is not mistaken for a command on the next loop.
    if ch in ("\x00", "\xe0"):
        msvcrt.getwch()
        return ""
    return ch


def _read_key_posix() -> str:
    import termios  # noqa: PLC0415 - POSIX-only import
    import tty  # noqa: PLC0415 - POSIX-only import

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        # Restore cooked mode after every key so blocking prompts (label/note
        # via input()) behave normally between keypresses.
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key_fallback() -> str:
    return sys.stdin.read(1)


def read_key() -> str:
    """Read and return a single keypress (``""`` on EOF).

    Picks a backend by OS, and falls back to a plain stdin read when the
    terminal cannot be put into single-key mode (e.g. stdin is not a TTY).
    """
    if not sys.stdin.isatty():
        return _read_key_fallback()
    try:
        if os.name == "nt":
            return _read_key_windows()
        return _read_key_posix()
    except Exception:
        return _read_key_fallback()
