"""Merge multiple recorded sessions into one continuous session.

``benchcam merge <id> <id> [...]`` is the stop/restart companion to capture: when
a bench story spans a stop-and-restart (a fresh session each time), this joins the
captures back into one continuous timeline and rebuilds the marker/mute-span axes
so the review renders as a single video. It is the long-deferred "pause / segment"
feature arriving from the other direction — instead of pausing one recording, you
record several and stitch them.

How it works, and the hard rules it enforces:

- STREAM COPY ONLY, never re-encode. The captures are joined with ffmpeg's concat
  demuxer (``-c copy``). That is only valid when every source shares codec /
  geometry / rate / audio layout, so stream parameters are probed and any mismatch
  (including one source having audio and another not) is a HARD refusal — never
  ``-c copy`` across differing parameters.
- SOURCES MUST BE ``ended``. A running/created session's capture is still being
  written: its length is a moving target and the concat would read a growing file.
  Refuse, naming the session.
- CONTINUOUS elapsed axis. Each source's marker ``elapsed_seconds`` and mute-span
  ``start_seconds``/``end_seconds`` are offset by the cumulative ffprobe duration of
  the PRIOR sources — the exact video length, not wall-clock (which includes ffmpeg
  startup and any STOP stall). ``marker_index`` and ``span_index`` are renumbered
  across the combined set. Mute spans get the identical offset to markers (they
  share the same axis); missing that silently force-timelapses the wrong regions.
- SOURCE ORDER is ARGUMENT order (explicit beats inferred). A loud warning names
  both orderings when arguments are not chronological; nothing is silently sorted.
- SOURCES ARE NEVER MODIFIED. Everything is written into a new merged folder; a bad
  merge costs a delete of that folder and nothing else.
- HARD POST-CONDITIONS: the merged length must equal the sum of source lengths
  within a small tolerance (a SHORT result means the join truncated → delete the
  partial output and refuse), and the merged video/audio stream ends are compared
  for A/V drift across the join (reported; a meaningful divergence is a finding, not
  silently re-encoded away).
- Only SOURCE-OF-TRUTH files are carried: capture, markers.csv, mute_spans.csv
  (offset + renumbered), notes.md (merged), session.json (rebuilt, with provenance).
  Derived artifacts (transcript.json, review.mp4, chapters.*, *.bak) are DROPPED —
  they describe a single source, and a stale transcript.json would poison the shared
  Whisper cache for the merged capture.
- BOM-free UTF-8 for every file written (a BOM crashed json.loads in the manual
  procedure this replaces).

Laptop-side: it operates on already-fetched local copies and is not part of capture.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .editor import (
    EditError,
    MUTE_SPANS_FILENAME,
    find_capture,
    probe_duration,
    resolve_session_dir,
)
from .markers import FIELDNAMES, MARKERS_FILENAME, read_markers
from .session import (
    NOTES_FILENAME,
    STATUS_ENDED,
    Session,
    load_session,
)

#: Merged length must equal the sum of source lengths within this many seconds.
#: The concat demuxer adds sub-frame container rounding at each join (observed
#: ~23ms for two 30fps sources); a truncated join drops seconds-to-minutes, so
#: this cleanly separates rounding from failure.
_DURATION_TOLERANCE = 0.5

#: Video and audio stream ends may differ by up to ~1 frame per join from
#: stream-copy boundary rounding. Beyond this we report a drift finding (never a
#: re-encode). Reported regardless; only exceeding it warns.
_AV_SYNC_TOLERANCE = 0.15

#: Headroom above the exact sum of source capture sizes (container overhead + the
#: small session files). The merged file is a stream copy, so the size estimate is
#: exact, unlike the capture-rate estimate the recorder's start guard must use.
_FREE_SPACE_HEADROOM = 64 * 1024 * 1024

#: mute_spans.csv columns (written by the Pi GPIO daemon). Only the two seconds
#: columns are offset and span_index renumbered; wall-time/source carry as-is.
_MUTE_SPAN_FIELDNAMES = [
    "start_seconds",
    "end_seconds",
    "start_wall_time",
    "end_wall_time",
    "span_index",
    "source",
]

_FFMPEG_HINT = (
    "ffmpeg/ffprobe were not found on PATH. 'benchcam merge' joins the session "
    "captures with ffmpeg's concat demuxer (stream copy) and probes durations with "
    "ffprobe. Install ffmpeg - Windows: 'winget install Gyan.FFmpeg'; Linux: "
    "'sudo apt install ffmpeg'; macOS: 'brew install ffmpeg'."
)


class MergeError(RuntimeError):
    """Raised for any problem merging sessions (refusals and failed joins)."""


@dataclass
class _Source:
    """One resolved source session with everything the merge needs from it."""

    session: Session
    folder: Path
    capture: Path
    streams: dict  # {"video": {...} | None, "audio": {...} | None}
    duration: float
    offset: float = 0.0

    @property
    def id(self) -> str:
        return self.session.session_id


# --------------------------------------------------------------------------- #
# Probing + compatibility (the hard precondition)
# --------------------------------------------------------------------------- #

def _probe_streams(capture: Path, ffprobe: str) -> dict:
    """Return the stream parameters that must match to stream-copy concat.

    ``{"video": {codec_name,width,height,r_frame_rate,pix_fmt} | None,
       "audio": {codec_name,sample_rate,channels} | None}``.
    """
    cmd = [
        ffprobe, "-v", "error", "-show_entries",
        "stream=codec_type,codec_name,width,height,r_frame_rate,pix_fmt,"
        "sample_rate,channels",
        "-of", "json", str(capture),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise MergeError(f"ffprobe could not read {capture}: {(result.stderr or '').strip()}")
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError as exc:
        raise MergeError(f"could not parse ffprobe output for {capture}.") from exc
    video = audio = None
    for st in data.get("streams", []):
        kind = st.get("codec_type")
        if kind == "video" and video is None:
            video = {k: st.get(k) for k in
                     ("codec_name", "width", "height", "r_frame_rate", "pix_fmt")}
        elif kind == "audio" and audio is None:
            audio = {k: st.get(k) for k in ("codec_name", "sample_rate", "channels")}
    return {"video": video, "audio": audio}


def _assert_compatible(sources: list[_Source]) -> None:
    """Refuse unless every source shares container + video + audio parameters.

    Includes stream-PRESENCE parity: one source with audio and another without
    cannot be ``-c copy`` concatenated (differing stream counts), so that is a
    refusal too — not only a parameter difference.
    """
    ref = sources[0]
    for s in sources[1:]:
        if s.capture.suffix.lower() != ref.capture.suffix.lower():
            raise MergeError(
                f"container mismatch: {ref.id} is '{ref.capture.suffix}' but {s.id} "
                f"is '{s.capture.suffix}'. Cannot stream-copy across containers."
            )
        for kind in ("video", "audio"):
            ref_params, s_params = ref.streams[kind], s.streams[kind]
            if (ref_params is None) != (s_params is None):
                have, lack = (ref.id, s.id) if ref_params else (s.id, ref.id)
                raise MergeError(
                    f"stream-presence mismatch: {have} has a {kind} stream, {lack} "
                    f"does not. Cannot -c copy concat sources with differing streams."
                )
            if ref_params is not None and ref_params != s_params:
                raise MergeError(
                    f"{kind} parameter mismatch between {ref.id} and {s.id}: "
                    f"{ref_params} vs {s_params}. Refusing to -c copy across "
                    f"differing parameters (would corrupt the merge)."
                )


# --------------------------------------------------------------------------- #
# Concat + post-conditions
# --------------------------------------------------------------------------- #

def _nearest_existing(path: Path) -> Path:
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _fmt_gb(n: float) -> str:
    return f"{n / (1024 ** 3):.2f} GB"


def _concat(sources: list[_Source], dest_capture: Path, ffmpeg: str,
            out: Callable[[str], object]) -> None:
    """Join the source captures into ``dest_capture`` with the concat demuxer.

    The list file is BOM-free UTF-8 with forward-slash absolute paths (ffmpeg on
    Windows accepts those) and ``-safe 0`` to allow absolute paths.
    """
    listing = "".join(f"file '{s.capture.resolve().as_posix()}'\n" for s in sources)
    fd, list_path = tempfile.mkstemp(suffix=".txt", prefix="benchcam-concat-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(listing)
        out(f"Concatenating {len(sources)} capture(s) into {dest_capture.name} "
            "(stream copy, no re-encode)...")
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy", str(dest_capture),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not dest_capture.exists():
            raise MergeError(f"ffmpeg concat failed: {(result.stderr or '').strip()}")
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass


def _stream_end_pts(path: Path, stream: str, duration: float, ffprobe: str) -> float | None:
    """Last packet ``pts_time`` for a stream, or None if the stream is absent.

    Reads only a ~20s window near the end (``-read_intervals``) so it stays fast on
    a large merged file. MKV per-stream ``duration`` is N/A and packet-count/fps is
    unreliable with dropped frames, so the last packet timestamp is the robust probe.
    """
    start = max(0.0, duration - 10.0)
    cmd = [
        ffprobe, "-v", "error", "-select_streams", stream,
        "-read_intervals", f"{start:.3f}%+20",
        "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    times = []
    for tok in (result.stdout or "").split():
        try:
            times.append(float(tok))
        except ValueError:
            continue
    return max(times) if times else None


def _check_av_sync(dest_capture: Path, duration: float, ffprobe: str,
                   out: Callable[[str], object]) -> None:
    """Report the merged video-vs-audio end drift; warn (never fail) past tolerance."""
    video_end = _stream_end_pts(dest_capture, "v:0", duration, ffprobe)
    audio_end = _stream_end_pts(dest_capture, "a:0", duration, ffprobe)
    if video_end is None or audio_end is None:
        out("A/V sync: video-only merge (no audio stream); skipped.")
        return
    delta = abs(video_end - audio_end)
    out(f"A/V sync: video ends {video_end:.3f}s, audio ends {audio_end:.3f}s "
        f"(delta {delta * 1000:.0f}ms).")
    if delta > _AV_SYNC_TOLERANCE:
        out(f"warning: audio and video diverge by {delta:.3f}s across the join(s) - "
            "inspect the merged audio (no re-encode was performed).")


# --------------------------------------------------------------------------- #
# Rewriting the timeline files (offset + renumber; sources untouched)
# --------------------------------------------------------------------------- #

def _write_merged_markers(sources: list[_Source], dest: Path,
                          out: Callable[[str], object]) -> int:
    """Combine markers across sources: offset elapsed, renumber index, carry rest."""
    rows: list[dict] = []
    for s in sources:
        for row in read_markers(s.folder / MARKERS_FILENAME):
            try:
                elapsed = float(row.get("elapsed_seconds"))
            except (TypeError, ValueError):
                continue  # unparseable row: skip rather than corrupt the merge
            new = dict(row)
            new["elapsed_seconds"] = f"{elapsed + s.offset:.3f}"
            rows.append(new)
    for i, row in enumerate(rows, 1):
        row["marker_index"] = i
    with (dest / MARKERS_FILENAME).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, "") for f in FIELDNAMES})
    out(f"Wrote {len(rows)} marker(s) on the merged timeline.")
    return len(rows)


def _write_merged_mute_spans(sources: list[_Source], dest: Path,
                             out: Callable[[str], object]) -> int:
    """Combine mute spans across sources: offset start/end, renumber span_index.

    No file is written when no source has any span (matching a session that never
    muted). Unknown extra columns are preserved.
    """
    combined: list[dict] = []
    for s in sources:
        for row in read_markers(s.folder / MUTE_SPANS_FILENAME):
            try:
                start = float(row.get("start_seconds"))
                end = float(row.get("end_seconds"))
            except (TypeError, ValueError):
                continue
            new = dict(row)
            new["start_seconds"] = f"{start + s.offset:.3f}"
            new["end_seconds"] = f"{end + s.offset:.3f}"
            combined.append(new)
    if not combined:
        return 0
    for i, row in enumerate(combined, 1):
        row["span_index"] = i
    fieldnames = list(_MUTE_SPAN_FIELDNAMES)
    for row in combined:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with (dest / MUTE_SPANS_FILENAME).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in combined:
            writer.writerow({f: row.get(f, "") for f in fieldnames})
    out(f"Wrote {len(combined)} mute span(s) on the merged timeline.")
    return len(combined)


def _notes_body(text: str) -> str:
    """The real content of a notes.md, minus the default ``# Notes for session`` header."""
    lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("# Notes for session ")
    ]
    return "\n".join(lines).strip()


def _write_merged_notes(sources: list[_Source], dest: Path) -> None:
    """Concatenate non-boilerplate notes with per-source headers; carry one if all boilerplate."""
    header = f"# Notes for session {dest.name}\n\n"
    real: list[tuple[_Source, str]] = []
    for s in sources:
        path = s.folder / NOTES_FILENAME
        body = _notes_body(path.read_text(encoding="utf-8")) if path.exists() else ""
        if body:
            real.append((s, body))
    if not real:
        content = header  # every source is the default template: carry one clean header
    else:
        parts = [f"## From {s.session.display_name} ({s.id})\n\n{body}" for s, body in real]
        content = header + "\n\n".join(parts) + "\n"
    (dest / NOTES_FILENAME).write_text(content, encoding="utf-8")


def _write_merged_session(sources: list[_Source], dest: Path, name: str) -> Session:
    """Rebuild session.json from the first source + last source's end + provenance."""
    first = sources[0].session
    last = sources[-1].session
    merged = Session(
        session_id=dest.name,
        created_wall_time=first.created_wall_time,
        profile=first.profile,
        camera=first.camera,
        microphone=first.microphone,
        recorder=first.recorder,
        storage_path=str(dest),
        notes=first.notes,
        name=(name or first.name),
        status=STATUS_ENDED,
        started_wall_time=first.started_wall_time,
        ended_wall_time=last.ended_wall_time,
        min_session_minutes=first.min_session_minutes,
        capture_rate_mb_per_min=first.capture_rate_mb_per_min,
        merged_from=[s.id for s in sources],
        merge_source_durations=[round(s.duration, 3) for s in sources],
    )
    merged.save()  # BOM-free UTF-8 (Session.save uses encoding='utf-8')
    return merged


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def _resolve_sources(specs, root: Path, ffprobe: str,
                     out: Callable[[str], object]) -> list[_Source]:
    """Resolve every source: load, refuse non-ended / capture-less, probe."""
    sources: list[_Source] = []
    for spec in specs:
        folder = resolve_session_dir(root, spec)  # EditError if not found
        session = load_session(folder)
        if session.status != STATUS_ENDED:
            raise MergeError(
                f"refusing to merge {session.session_id!r}: status is "
                f"{session.status!r}, not 'ended'. Its capture is still being "
                "written (length is a moving target) - end it first."
            )
        try:
            capture = find_capture(folder)
        except EditError as exc:
            raise MergeError(
                f"{session.session_id!r} has no local capture file to merge ({exc})."
            ) from exc
        streams = _probe_streams(capture, ffprobe)
        if streams["video"] is None:
            raise MergeError(f"{session.session_id!r} capture has no video stream to merge.")
        duration = probe_duration(capture, ffprobe=ffprobe)
        sources.append(_Source(session, folder, capture, streams, duration))
    return sources


def _warn_out_of_order(sources: list[_Source], out: Callable[[str], object]) -> None:
    """Loudly warn (do not sort) when argument order isn't chronological."""
    def key(s: _Source):
        return s.session.started_wall_time or s.session.created_wall_time or ""
    chronological = sorted(sources, key=key)
    if [s.id for s in chronological] != [s.id for s in sources]:
        out("warning: source order does NOT match chronological (started_wall_time) order.")
        out(f"  argument order:      {' -> '.join(s.id for s in sources)}")
        out(f"  chronological order: {' -> '.join(s.id for s in chronological)}")
        out("  Using ARGUMENT order (explicit). Re-order the arguments if that is "
            "not what you meant.")


def _warn_metadata_divergence(sources: list[_Source], out: Callable[[str], object]) -> None:
    for name in ("profile", "camera", "microphone"):
        values = {getattr(s.session, name) for s in sources}
        if len(values) > 1:
            out(f"warning: sources differ in {name} ({sorted(values)}); using "
                f"{sources[0].id}'s value.")


def run_merge(
    sources: list[str],
    *,
    sessions_root: Path | str,
    name: str = "",
    out: Callable[[str], object] = print,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> Path:
    """Merge ``sources`` (session ids/paths, in order) into a new merged session.

    Returns the merged session folder. Refuses (writing nothing) on: fewer than two
    sources, a non-ended source, a missing/incompatible capture, or insufficient free
    space. After the concat, a short merged duration deletes the partial output and
    refuses. Sources are never modified.
    """
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    ffprobe = ffprobe or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise MergeError(_FFMPEG_HINT)
    if len(sources) < 2:
        raise MergeError("merge needs at least two sessions (nothing to join otherwise).")

    root = Path(sessions_root)
    srcs = _resolve_sources(sources, root, ffprobe, out)

    # Cumulative offsets: the running prefix sum of prior durations (source i's
    # video starts at sum(d[0..i-1]) in the merged timeline).
    acc = 0.0
    for s in srcs:
        s.offset = acc
        acc += s.duration
    expected_total = acc

    _warn_out_of_order(srcs, out)
    _warn_metadata_divergence(srcs, out)
    _assert_compatible(srcs)  # hard precondition — refuses before any write

    required = sum(s.capture.stat().st_size for s in srcs)
    free = shutil.disk_usage(_nearest_existing(root)).free
    if free < required + _FREE_SPACE_HEADROOM:
        raise MergeError(
            f"not enough free space to merge on {root}: need ~{_fmt_gb(required)} "
            f"(+ headroom), {_fmt_gb(free)} free."
        )

    dest = _unique_merged_folder(root, srcs[0].id)
    dest.mkdir(parents=True, exist_ok=False)
    try:
        dest_capture = dest / f"capture{srcs[0].capture.suffix}"
        _concat(srcs, dest_capture, ffmpeg, out)

        merged_dur = probe_duration(dest_capture, ffprobe=ffprobe)
        shortfall = expected_total - merged_dur
        if shortfall > _DURATION_TOLERANCE:
            raise MergeError(
                f"merged duration {merged_dur:.3f}s is short of the expected "
                f"{expected_total:.3f}s (sum of sources) by {shortfall:.3f}s - the "
                "concat truncated. Deleting the partial merge."
            )
        if merged_dur - expected_total > _DURATION_TOLERANCE:
            out(f"warning: merged duration {merged_dur:.3f}s exceeds expected "
                f"{expected_total:.3f}s by {merged_dur - expected_total:.3f}s "
                "(unusual; not truncation).")
        else:
            out(f"Duration OK: merged {merged_dur:.3f}s vs expected "
                f"{expected_total:.3f}s (delta {abs(merged_dur - expected_total) * 1000:.0f}ms).")

        _check_av_sync(dest_capture, merged_dur, ffprobe, out)

        _write_merged_markers(srcs, dest, out)
        _write_merged_mute_spans(srcs, dest, out)
        _write_merged_notes(srcs, dest)
        _write_merged_session(srcs, dest, name)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)  # never leave a partial merge behind
        raise

    out(f"Merged {len(srcs)} session(s) into {dest} "
        f"(offsets: {', '.join(f'{s.offset:.3f}s' for s in srcs)}).")
    return dest


def _unique_merged_folder(root: Path, first_id: str) -> Path:
    """A non-existing ``<first_id>_merged`` folder (numeric suffix on collision)."""
    base = f"{first_id}_merged"
    candidate = root / base
    counter = 2
    while candidate.exists():
        candidate = root / f"{base}_{counter}"
        counter += 1
    return candidate
