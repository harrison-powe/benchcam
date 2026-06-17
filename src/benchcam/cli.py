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
from . import artifacts as artifacts_mod
from . import session as session_mod
from .artifacts import ArtifactError
from .inputs.keyboard_input import run_interactive_loop
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
    p_run.add_argument(
        "--interactive",
        action="store_true",
        help="Open a line-based prompt to log markers/notes without re-running.",
    )
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
    p_mark.add_argument(
        "--note",
        default="",
        help="Optional free-text note for this marker.",
    )
    p_mark.set_defaults(func=cmd_mark)

    # end
    p_end = sub.add_parser("end", help="Stop recording and close the session.")
    _add_root_arg(p_end)
    p_end.set_defaults(func=cmd_end)

    # status
    p_status = sub.add_parser(
        "status", help="Show a summary of a session (active by default)."
    )
    _add_root_arg(p_status)
    p_status.add_argument(
        "--session",
        default=None,
        help="Path to a specific session folder (defaults to the active session).",
    )
    p_status.set_defaults(func=cmd_status)

    # attach-media
    p_attach = sub.add_parser(
        "attach-media",
        help="Attach an externally recorded media file to a session.",
    )
    _add_root_arg(p_attach)
    p_attach.add_argument("file_path", help="Path to the media file to attach.")
    p_attach.add_argument(
        "--session",
        default=None,
        help="Path to a specific session folder (defaults to the active session).",
    )
    p_attach.add_argument("--label", default="", help="Optional label for the media.")
    p_attach.add_argument(
        "--kind",
        default=None,
        choices=list(artifacts_mod.KINDS),
        help="Media kind (inferred from the extension when omitted).",
    )
    p_attach.add_argument(
        "--mode",
        default=artifacts_mod.MODE_COPY,
        choices=list(artifacts_mod.MODES),
        help="copy into the session (default) or reference in place.",
    )
    p_attach.set_defaults(func=cmd_attach_media)

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

    already_running = session.status == session_mod.STATUS_RUNNING
    if already_running:
        print(f"Session {session.session_id} is already running.")
    else:
        recorder.start(session.folder)
        session_mod.start_session(session)
        print(
            f"Recording session {session.session_id} "
            f"(recorder: {session.recorder})."
        )
        if session.recorder == "null":
            print(
                "  NullRecorder: no video is being captured. Capture manually if "
                "needed; markers and timing are still logged."
            )

    if getattr(args, "interactive", False):
        run_interactive_loop(session, recorder)

    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    session = session_mod.get_active_session(Path(args.sessions_root))
    marker = session_mod.add_marker(
        session, args.label, source=args.source, note=args.note
    )
    line = (
        f"Marker #{marker.marker_index} @ {marker.elapsed_seconds:.3f}s "
        f"[{marker.source}] {marker.label}"
    )
    if marker.note:
        line += f" -- {marker.note}"
    print(line)
    return 0


def cmd_end(args: argparse.Namespace) -> int:
    session = session_mod.get_active_session(Path(args.sessions_root))
    recorder = get_recorder(session.recorder)
    recorder.stop()
    session_mod.end_session(session)
    print(f"Ended session {session.session_id}.")
    print(f"  markers: {session.markers_file}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if args.session:
        session = session_mod.load_session(Path(args.session))
    else:
        session = session_mod.get_active_session(Path(args.sessions_root))

    print(session_mod.status_summary(session))
    return 0


def cmd_attach_media(args: argparse.Namespace) -> int:
    if args.session:
        session = session_mod.load_session(Path(args.session))
    else:
        session = session_mod.get_active_session(Path(args.sessions_root))

    artifact = artifacts_mod.attach_media(
        session,
        Path(args.file_path),
        label=args.label,
        kind=args.kind,
        mode=args.mode,
    )
    print(f"Attached media to session {session.session_id} ({artifact.mode}).")
    print(f"  session: {session.storage_path}")
    if artifact.mode == artifacts_mod.MODE_COPY:
        print(f"  stored:  {session.folder / artifact.stored_path}")
    else:
        print(f"  referenced: {artifact.original_path}")
    print(f"  manifest: {session.artifacts_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SessionError, RecorderError, ArtifactError) as exc:
        print(f"benchcam: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
