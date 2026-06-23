"""BenchCam command-line interface.

Commands:
    benchcam new            create a new session (folder + files), make it active
    benchcam run            start recording for the active session
    benchcam mark "label"   log a time-stamped marker
    benchcam live           interactive single-keypress marking shell
    benchcam end            stop recording and close the active session
    benchcam edit           render a marker-aware review.mp4 for a session
    benchcam transcribe     auto-label markers from spoken narration (Whisper)
    benchcam dashboard      local web UI: start/mark/stop/review in a browser

Each command is a separate process, so the "active" session is tracked on disk
via a small pointer file (see session.py). State lives in plain local files.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from . import dashboard as dashboard_mod
from . import editor as editor_mod
from . import keypress
from . import live as live_mod
from . import session as session_mod
from . import transcribe as transcribe_mod
from .dashboard import DashboardError
from .editor import EditError
from .recorders import get_recorder
from .recorders.base import RecorderError
from .session import SessionError
from .transcribe import TranscribeError


ENV_SESSIONS_ROOT = "BENCHCAM_SESSIONS_ROOT"


def _default_sessions_root() -> str:
    """Resolve the default sessions root: $BENCHCAM_SESSIONS_ROOT, else ./sessions.

    Lets all data (sessions, markers, and collected OBS videos) live on, e.g., an
    external SSD by setting the env var once — no per-command flag and no code
    change. An explicit --sessions-root still wins.
    """
    return os.environ.get(ENV_SESSIONS_ROOT) or str(session_mod.DEFAULT_SESSIONS_ROOT)


def _add_root_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sessions-root",
        default=_default_sessions_root(),
        help=(
            "Directory that holds session folders "
            "(default: $BENCHCAM_SESSIONS_ROOT or ./sessions)."
        ),
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

    # live
    p_live = sub.add_parser(
        "live",
        help="Interactive shell: mark the active session on a single keypress.",
    )
    _add_root_arg(p_live)
    p_live.set_defaults(func=cmd_live)

    # edit
    p_edit = sub.add_parser(
        "edit",
        help="Render a marker-aware review.mp4 (timelapse + normal-speed marker "
        "windows + captions) from a session.",
    )
    _add_root_arg(p_edit)
    p_edit.add_argument(
        "--session",
        default=None,
        help="Session id or folder path to edit (default: newest session).",
    )
    p_edit.add_argument(
        "--pre",
        type=float,
        default=editor_mod.DEFAULT_PRE,
        help="Seconds of normal speed before each marker (default: 3).",
    )
    p_edit.add_argument(
        "--post",
        type=float,
        default=editor_mod.DEFAULT_POST,
        help="Seconds of normal speed after each marker (default: 5).",
    )
    p_edit.add_argument(
        "--speed",
        type=float,
        default=editor_mod.DEFAULT_SPEED,
        help="Timelapse factor for the stretches between markers (default: 8).",
    )
    p_edit.add_argument(
        "--font",
        default=None,
        help="Path to a .ttf font for captions (default: a system font).",
    )
    p_edit.set_defaults(func=cmd_edit)

    # transcribe
    p_tr = sub.add_parser(
        "transcribe",
        help="Auto-label markers from spoken narration using Whisper "
        "(runs on the laptop; needs the [transcribe] extra).",
    )
    _add_root_arg(p_tr)
    p_tr.add_argument(
        "--session",
        default=None,
        help="Session id or folder path to transcribe (default: newest session).",
    )
    p_tr.add_argument(
        "--model",
        default=None,
        help=(
            "Whisper model name, e.g. tiny/base/small/medium/large "
            f"(default: $BENCHCAM_WHISPER_MODEL or {transcribe_mod.DEFAULT_MODEL})."
        ),
    )
    p_tr.add_argument(
        "--window",
        type=float,
        default=transcribe_mod.DEFAULT_WINDOW,
        help="Seconds before/after each marker to pull narration from (default: 5).",
    )
    p_tr.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing marker labels too (default: only fill empty ones).",
    )
    p_tr.set_defaults(func=cmd_transcribe)

    # dashboard
    p_dash = sub.add_parser(
        "dashboard",
        help="Start a local web dashboard (start/mark/stop/review in a browser).",
    )
    _add_root_arg(p_dash)
    p_dash.add_argument(
        "--host", default=dashboard_mod.DEFAULT_HOST, help="Bind host (default: 127.0.0.1)."
    )
    p_dash.add_argument(
        "--port", type=int, default=dashboard_mod.DEFAULT_PORT,
        help="Bind port (default: 8765).",
    )
    p_dash.add_argument(
        "--no-browser", action="store_true",
        help="Do not open a browser window automatically.",
    )
    p_dash.set_defaults(func=cmd_dashboard)

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
    # cmd_run started ffmpeg in a *different* process, so this fresh recorder has
    # no in-memory handle; pass the session folder so it can stop the capture via
    # the persisted ffmpeg.pid instead of leaving it orphaned.
    recorder.stop(session.folder)
    session_mod.end_session(session)
    print(f"Ended session {session.session_id}.")
    print(f"  markers: {session.markers_file}")
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    session = session_mod.get_active_session(Path(args.sessions_root))
    recorder = get_recorder(session.recorder)
    return live_mod.live_session(
        session,
        recorder=recorder,
        read_key=keypress.read_key,
    )


def cmd_edit(args: argparse.Namespace) -> int:
    session_dir = editor_mod.resolve_session_dir(
        Path(args.sessions_root), args.session
    )
    output = editor_mod.run_edit(
        session_dir,
        pre=args.pre,
        post=args.post,
        speed=args.speed,
        font=args.font,
    )
    print(f"Wrote {output}")
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    session_dir = editor_mod.resolve_session_dir(
        Path(args.sessions_root), args.session
    )
    transcribe_mod.run_transcribe(
        session_dir,
        model=args.model,
        window=args.window,
        overwrite=args.overwrite,
    )
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    return dashboard_mod.serve(
        host=args.host,
        port=args.port,
        sessions_root=Path(args.sessions_root),
        open_browser=not args.no_browser,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        SessionError,
        RecorderError,
        EditError,
        DashboardError,
        TranscribeError,
    ) as exc:
        print(f"benchcam: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
