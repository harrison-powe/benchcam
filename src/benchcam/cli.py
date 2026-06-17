"""BenchCam command-line interface.

Commands:
    benchcam new            create a new session (folder + files), make it active
    benchcam run            start recording for the active session
    benchcam mark "label"   log a time-stamped marker
    benchcam end            stop recording and close the active session

Each command is a separate process, so the "active" session is tracked on disk
via a small pointer file (see session.py). State lives in plain local files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from . import session as session_mod
from .recorders import get_recorder
from .recorders.base import RecorderError
from .session import SessionError


def _add_root_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sessions-root",
        default=str(session_mod.DEFAULT_SESSIONS_ROOT),
        help="Directory that holds session folders (default: ./sessions).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchcam",
        description="Local-first bench-side capture and marker logging.",
    )
    parser.add_argument(
        "--version", action="version", version=f"benchcam {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # new
    p_new = sub.add_parser("new", help="Create a new session and make it active.")
    _add_root_arg(p_new)
    p_new.add_argument("--profile", default="default", help="Profile name.")
    p_new.add_argument("--camera", default="", help="Camera description/device.")
    p_new.add_argument(
        "--microphone", default="", help="Microphone description/device."
    )
    p_new.add_argument(
        "--recorder",
        default="null",
        choices=["null", "obs", "ffmpeg"],
        help="Recorder backend (default: null).",
    )
    p_new.add_argument("--notes", default="", help="Initial notes text.")
    p_new.set_defaults(func=cmd_new)

    # run
    p_run = sub.add_parser("run", help="Start recording for the active session.")
    _add_root_arg(p_run)
    p_run.set_defaults(func=cmd_run)

    # mark
    p_mark = sub.add_parser("mark", help="Log a time-stamped marker.")
    _add_root_arg(p_mark)
    p_mark.add_argument("label", help="Marker label, e.g. \"chip lifted\".")
    p_mark.add_argument(
        "--source",
        default="manual",
        help="Where the marker came from (default: manual).",
    )
    p_mark.set_defaults(func=cmd_mark)

    # end
    p_end = sub.add_parser("end", help="Stop recording and close the session.")
    _add_root_arg(p_end)
    p_end.set_defaults(func=cmd_end)

    return parser


def cmd_new(args: argparse.Namespace) -> int:
    session = session_mod.create_session(
        root=Path(args.sessions_root),
        profile=args.profile,
        camera=args.camera,
        microphone=args.microphone,
        recorder=args.recorder,
        notes=args.notes,
    )
    print(f"Created session {session.session_id}")
    print(f"  folder:   {session.storage_path}")
    print(f"  recorder: {session.recorder}")
    print("Run 'benchcam run' to start, then 'benchcam mark \"label\"'.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    session = session_mod.get_active_session(Path(args.sessions_root))
    recorder = get_recorder(session.recorder)
    recorder.start(session.folder)
    session_mod.start_session(session)
    print(f"Recording session {session.session_id} (recorder: {session.recorder}).")
    if session.recorder == "null":
        print(
            "  NullRecorder: no video is being captured. Capture manually if "
            "needed; markers and timing are still logged."
        )
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    session = session_mod.get_active_session(Path(args.sessions_root))
    marker = session_mod.add_marker(session, args.label, source=args.source)
    print(
        f"Marker #{marker.marker_index} @ {marker.elapsed_seconds:.3f}s "
        f"[{marker.source}] {marker.label}"
    )
    return 0


def cmd_end(args: argparse.Namespace) -> int:
    session = session_mod.get_active_session(Path(args.sessions_root))
    recorder = get_recorder(session.recorder)
    recorder.stop()
    session_mod.end_session(session)
    print(f"Ended session {session.session_id}.")
    print(f"  markers: {session.markers_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SessionError, RecorderError) as exc:
        print(f"benchcam: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
