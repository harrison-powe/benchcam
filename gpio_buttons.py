#!/usr/bin/env python3
"""BenchCam GPIO control daemon (Pi-only): four physical buttons + two status LEDs.
  START GPIO27 -> create session + start recording (writes ffmpeg.pid)
  MARK  GPIO17 -> drop a gpio marker into the running session
  STOP  GPIO22 -> stop recording (kills ffmpeg via pidfile) + end session
  MUTE  GPIO23 -> toggle a "mute span" (force-timelapse region); LED ON while muted
  LED   GPIO24 -> mute status LED: ON while a mute span is open, OFF otherwise
  LED   GPIO5  -> record status LED: ON while a session is RUNNING, OFF otherwise
  LIGHT GPIO6  -> bench ring light via an IRLZ44N low-side MOSFET; ON while a
                  session is RUNNING. An illumination LOAD, not an indicator —
                  see _shutdown for why it is not cleared on stop.
  OLED  I2C-1  -> SSD1306 128x64 at 0x3C (J2): elapsed, marks, state, free space
Reuses the CLI run/end paths, so START/STOP are cross-process-safe. Mute spans
are the mirror of a marker: a start/end pair of elapsed_seconds written to
mute_spans.csv, measured against the SAME on-disk baseline markers use.

The OLED is driven by a 1 Hz background thread (the buttons stay event-driven;
gpiozero dispatches their callbacks on its own threads, so the display never
delays a press). It is strictly a READER of state and is wrapped so that no
display fault can affect capture."""
import csv
import json
import os
import shutil
import signal
import threading
import time
from pathlib import Path
from signal import pause
from gpiozero import Button, DigitalOutputDevice, LED
from benchcam import clock
from benchcam.markers import read_markers
from benchcam.session import (
    create_session, get_active_session, start_session, end_session,
    add_marker, elapsed_seconds, SessionError, STATUS_RUNNING,
)
from benchcam.recorders import get_recorder
# PUBLIC names only: the pre-flight floor the recorder enforces, so the display
# can never show a healthy number while START would refuse. The recorder's
# _existing_ancestor/_format_gb are deliberately NOT imported — this file is not
# in git and not part of the package, so a rename there would break it silently;
# the few lines they represent are duplicated locally instead.
from benchcam.recorders.ffmpeg import (
    required_free_bytes, DEFAULT_MIN_SESSION_MINUTES,
    DEFAULT_CAPTURE_RATE_MB_PER_MIN, ENV_MIN_SESSION_MINUTES,
    ENV_CAPTURE_RATE_MB_MIN,
)

# luma/PIL are optional at runtime: if they are missing the daemon must still
# record and serve buttons, so the import itself is fail-soft.
try:
    from luma.core.interface.serial import i2c
    from luma.core.render import canvas
    from luma.oled.device import ssd1306
    from PIL import ImageFont
    _OLED_IMPORTS_OK = True
except Exception as _oled_import_err:  # noqa: BLE001 - display is never fatal
    _OLED_IMPORTS_OK = False

ROOT = Path(os.environ.get("BENCHCAM_SESSIONS_ROOT",
                           str(Path.home() / "benchcam" / "sessions")))
RECORDER = "ffmpeg"  # Pi capture path (create_session defaults to "null" = no video)

# --- OLED status display (SSD1306 128x64, I2C, J2 on the v1 control PCB) -----
OLED_PORT = 1
OLED_ADDRESS = 0x3C
OLED_WIDTH, OLED_HEIGHT = 128, 64
DISPLAY_INTERVAL = 1.0     # seconds between frames (elapsed ticks at ~1 Hz)
FREE_SPACE_EVERY = 10      # re-probe free space every Nth tick (it moves slowly)
OLED_RETRY_SECONDS = 30.0  # re-init cadence while failing (loose Dupont jumper)
# Bound on the shutdown panel clear. Measured: an absent I2C device NACKs in
# under 5 ms, so this is ~400x the realistic worst case and can never approach
# systemd's TimeoutStopSec (90s) — it only ever matters if the refresh thread is
# mid-write when the signal lands.
PANEL_CLEAR_TIMEOUT = 2.0
FONT_BIG_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SMALL_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# --- mute spans (mirror of markers.csv, written by this daemon only) ---------
MUTE_SPANS_FILENAME = "mute_spans.csv"
MUTE_FIELDNAMES = [
    "start_seconds", "end_seconds", "start_wall_time", "end_wall_time",
    "span_index", "source",
]
#: Daemon-private record of the CURRENTLY-OPEN span, so a restart can resume it.
#: Deliberately NOT a row in mute_spans.csv: that file stays append-only and
#: complete-rows-only, so editor.py and merge.py only ever see FINISHED spans.
#: An in-flight span can therefore never be mistaken for one running to
#: end-of-session — the failure that would force-lapse the rest of the video.
MUTE_OPEN_FILENAME = "mute_open.json"

# Toggle state for the currently-open mute span. Held in module-level vars for
# the same reason the Button/LED objects are: this is one long-lived process and
# it is the only writer of mute_spans.csv. `_mute_session` is the Session that
# owns the open span, so we can close it even after it stops being the active
# one. `_mute_last_elapsed` is the freshest elapsed we measured while recording,
# used to close a span safely if the session stops out from under us.
_mute_open = False
_mute_session = None
_mute_start_seconds = 0.0
_mute_start_wall = ""
_mute_last_elapsed = 0.0

# OLED state. `_oled` holds the luma device at module level for the same reason
# the Button/LED objects are held: nothing else keeps it alive. `_oled_failing`
# makes logging TRANSITION-ONLY (one line per failure episode, not one per
# second). Only the display thread writes these.
_oled = None
_oled_failing = False
_oled_last_attempt = 0.0
_font_big = None
_font_small = None
_free_text = "--"
_free_low = False
_tick = 0

# Shutdown coordination. `_stopping` stops the refresh thread drawing again;
# `_panel_lock` makes the handler's clear and a tick-in-flight mutually
# exclusive, so a frame already being flushed can't repaint over the clear.
_stopping = False
_panel_lock = threading.Lock()


def _mute_spans_path(session):
    return session.folder / MUTE_SPANS_FILENAME


def _mute_open_path(session):
    return session.folder / MUTE_OPEN_FILENAME


def _unlink_quiet(path):
    try:
        path.unlink()
    except OSError:
        pass


def _persist_open_span(session, elapsed, wall_iso):
    """Record the in-flight span so a daemon restart can resume it.

    Best-effort: a write failure must never break MUTE or affect capture, so it
    is reported and the daemon carries on with the span held in memory only
    (i.e. degrades to the old lose-it-on-restart behaviour).
    """
    try:
        _mute_open_path(session).write_text(
            json.dumps({
                "start_seconds": round(float(elapsed), 3),
                "start_wall_time": wall_iso,
                "session_id": session.session_id,
                "source": "gpio",
            }) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as e:  # noqa: BLE001 - never fatal
        print(f"[gpio] MUTE: could not persist the open span — {e} "
              "(it will be lost if the daemon restarts).")


def _clear_open_span(session):
    """Drop the in-flight record once the span is committed (or discarded)."""
    _unlink_quiet(_mute_open_path(session))


def _next_span_index(path):
    if not path.exists():
        return 1
    with path.open("r", newline="", encoding="utf-8") as fh:
        return sum(1 for _ in csv.DictReader(fh)) + 1


def _append_mute_span(session, start_seconds, end_seconds, start_wall, end_wall):
    """Append one COMPLETE span row (atomic), creating the file+header lazily."""
    path = _mute_spans_path(session)
    exists = path.exists()
    index = _next_span_index(path)
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=MUTE_FIELDNAMES)
        if not exists:
            writer.writeheader()
        writer.writerow({
            "start_seconds": f"{start_seconds:.3f}",
            "end_seconds": f"{end_seconds:.3f}",
            "start_wall_time": start_wall,
            "end_wall_time": end_wall,
            "span_index": index,
            "source": "gpio",
        })
    return index


def _active_or_none():
    """The active session (any status) via the .active pointer, or None."""
    try:
        return get_active_session(ROOT)
    except SessionError:
        return None


def _running(active):
    """Narrow a loaded session to one that is actually RUNNING, else None."""
    return active if (active is not None and active.status == STATUS_RUNNING) else None


def _sync_recording_outputs(active):
    """Drive the RECORD LED and the RING LIGHT from the on-disk truth.

    Both ON iff a session is RUNNING. They share ONE evaluation of
    _running(active) precisely so they can never disagree — a lit bench beside an
    IDLE indicator would be genuinely confusing to read at the bench.

    A pure function of the already-loaded `active`, so there is no second source
    of truth to go stale — recording state lives in .active/session.json, not in
    this process (unlike the mute span, which is in-memory until it closes).
    Called wherever a handler has already loaded `active`, so it costs no extra
    disk read. A session ended OUT OF BAND (dashboard, CLI) is corrected on the
    next button press — the same staleness bound the mute LED already has; there
    is deliberately no background poll.

    Derivation is shared here; SHUTDOWN POLICY is not — _shutdown clears the LED
    and deliberately leaves the light alone. See there.
    """
    if _running(active) is not None:
        record_led.on()
        ring_light.on()
    else:
        record_led.off()
        ring_light.off()


# --- OLED: read-only view of session state, never a capture dependency -------

def _fmt_elapsed(seconds):
    """MM:SS that ROLLS PAST 59 minutes — a 90-minute take reads 90:12."""
    total = int(max(seconds, 0.0))
    return f"{total // 60:02d}:{total % 60:02d}"


def _free_bytes(path):
    """Free bytes on the mount holding `path`, walking up to an existing dir.

    A deliberate local duplicate of the recorder's pre-flight probe (rather than
    importing its underscore-private helpers): a rename there would break this
    untracked, uncovered file silently. Six lines is the cheaper risk.
    """
    p = Path(path)
    while not p.exists():
        parent = p.parent
        if parent == p:
            break
        p = parent
    return shutil.disk_usage(p).free


def _fmt_gb(num_bytes):
    """Decimal GB, matching how cards/SSDs are labeled (and the recorder's text)."""
    return f"{num_bytes / 1_000_000_000:.1f} GB"


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


def _free_floor_bytes():
    """The same floor start() enforces, so the display can't contradict it.

    Resolves env > default via the recorder's PUBLIC knobs. The recorder also
    honours per-session session.json overrides; this matches every case except a
    session that overrode the knobs in its own session.json.
    """
    return required_free_bytes(
        _env_float(ENV_CAPTURE_RATE_MB_MIN, DEFAULT_CAPTURE_RATE_MB_PER_MIN),
        _env_float(ENV_MIN_SESSION_MINUTES, DEFAULT_MIN_SESSION_MINUTES),
    )


def _refresh_free_space():
    """Re-probe free space and whether it is below the pre-flight floor."""
    global _free_text, _free_low
    free = _free_bytes(ROOT)
    floor = _free_floor_bytes()
    _free_text = _fmt_gb(free)
    _free_low = floor > 0 and free < floor


def _marker_count(session):
    """Marker count from the same on-disk source the dashboard uses.

    Returns None (rendered '-') if markers.csv can't be read — e.g. a tick that
    lands while another process is REWRITING it (update_marker). One dashed
    field beats a skipped frame.
    """
    try:
        return len(read_markers(session.markers_file))
    except Exception:  # noqa: BLE001 - a display read must never propagate
        return None


def _load_fonts():
    """Load the TTFs once; degrade to PIL's built-in rather than losing the OLED."""
    global _font_big, _font_small
    if _font_big is not None:
        return
    try:
        _font_big = ImageFont.truetype(FONT_BIG_PATH, 20)
        _font_small = ImageFont.truetype(FONT_SMALL_PATH, 11)
    except Exception:  # noqa: BLE001 - a missing font must not disable the display
        _font_big = _font_small = ImageFont.load_default()


def _release_oled(device):
    """Close a luma device's I2C fd. NOTHING ELSE WILL — do not remove.

    luma closes the bus only in cleanup(); neither luma's i2c nor smbus2.SMBus
    defines __del__, and the fd is a raw os.open() int, so dropping the last
    reference leaks it for the life of the process.

    persist=True is deliberate: luma's cleanup() is `if not persist: hide();
    clear()` followed by the serial close. On an already-dead panel hide()
    raises, which would skip the serial close underneath and leave the fd leaked
    anyway — exactly the case this is called in.
    """
    if device is None:
        return
    try:
        device.persist = True
        device.cleanup()
    except Exception:  # noqa: BLE001 - releasing must never raise
        pass


def _init_oled():
    """Construct the SSD1306, or return None if it can't be reached. Never raises.

    The serial interface is held in a local so the FAILURE path can close it.
    luma's i2c opens /dev/i2c-N in __init__ and does NOT probe the address, so
    construction succeeds even with nothing on the bus; the NACK only surfaces
    inside ssd1306(), by which point the bus is already open. Without the
    cleanup below, every failed retry leaked one fd until the 1024-fd limit was
    hit, after which /dev/i2c-N could not be opened AT ALL and the 30s retry
    could never recover — silently, since logging is transition-only.
    """
    if not _OLED_IMPORTS_OK:
        return None
    serial = None
    try:
        serial = i2c(port=OLED_PORT, address=OLED_ADDRESS)
        device = ssd1306(serial, width=OLED_WIDTH, height=OLED_HEIGHT)
        _load_fonts()
        return device  # the device owns `serial` now — must NOT be cleaned up
    except Exception:  # noqa: BLE001 - unplugged/absent module is not fatal
        if serial is not None:
            try:
                serial.cleanup()  # i2c.cleanup() -> self._bus.close()
            except Exception:  # noqa: BLE001
                pass
        return None


def _right_x(draw, text, font):
    """x coordinate that right-aligns `text` against the panel edge."""
    try:
        width = draw.textlength(text, font=font)
    except AttributeError:  # very old Pillow
        width = font.getsize(text)[0]
    return max(OLED_WIDTH - int(width), 0)


def _render(draw, st):
    """Draw one frame from already-gathered state (no I/O in here)."""
    # Row 1: state word + the big elapsed clock, right-aligned.
    draw.text((0, 6), "REC" if st["recording"] else "IDLE", font=_font_small, fill="white")
    draw.text((_right_x(draw, st["elapsed"], _font_big), 0), st["elapsed"],
              font=_font_big, fill="white")
    draw.line((0, 25, OLED_WIDTH - 1, 25), fill="white")
    # Row 2: marker count, plus a MUTE badge only while a span is open.
    marks = "-" if st["marks"] is None else str(st["marks"])
    draw.text((0, 30), f"marks {marks}" if st["recording"] else "ready",
              font=_font_small, fill="white")
    if st["muted"]:
        draw.text((_right_x(draw, "[MUTE]", _font_small), 30), "[MUTE]",
                  font=_font_small, fill="white")
    # Row 3: free space, flagged LOW below the pre-flight floor so the display
    # never looks healthy while START would refuse.
    draw.text((0, 48), f"free {st['free']}", font=_font_small, fill="white")
    if st["low"]:
        draw.text((_right_x(draw, "LOW", _font_small), 48), "LOW",
                  font=_font_small, fill="white")


def _display_tick():
    """Gather state and draw one frame. Session state is re-read every tick (the
    on-disk value is the cross-process truth); free space is throttled."""
    global _tick
    active = _active_or_none()
    s = _running(active)
    if _tick % FREE_SPACE_EVERY == 0:
        _refresh_free_space()
    _tick += 1
    state = {
        "recording": s is not None,
        "elapsed": _fmt_elapsed(elapsed_seconds(s)) if s is not None else "--:--",
        "marks": _marker_count(s) if s is not None else None,
        "muted": _mute_open,   # plain bool read; atomic under the GIL, no lock
        "free": _free_text,
        "low": _free_low,
    }
    # Under the lock so a shutdown clear can't interleave with this flush.
    with _panel_lock:
        with canvas(_oled) as draw:
            _render(draw, state)


def _display_loop():
    """1 Hz refresh thread. NEVER raises — the OLED must not be able to stop
    capture or the buttons. Logs only on health TRANSITIONS, so a yanked jumper
    costs one journal line, not one per second."""
    global _oled, _oled_failing, _oled_last_attempt
    while True:
        if _stopping:
            return  # shutting down: never repaint after the handler's clear
        try:
            if _oled is None:
                now = time.monotonic()
                if now - _oled_last_attempt >= OLED_RETRY_SECONDS:
                    _oled_last_attempt = now
                    _oled = _init_oled()  # a re-seated jumper recovers here
            if _oled is not None:
                _display_tick()
                if _oled_failing:
                    _oled_failing = False
                    print("[gpio] OLED recovered.")
        except Exception as e:  # noqa: BLE001 - display faults are never fatal
            # Release before dropping the reference: a device that worked and
            # then failed mid-flight (yanked jumper) still holds an open I2C fd,
            # and nothing else closes it. Without this, one live failure plus the
            # retries after it leak an fd apiece toward the 1024 limit.
            _release_oled(_oled)
            _oled = None
            _oled_last_attempt = time.monotonic()
            if not _oled_failing:
                _oled_failing = True
                print(f"[gpio] OLED failed — {e} (display off; capture unaffected)")
        time.sleep(DISPLAY_INTERVAL)


def _shutdown(signum, _frame):
    """Leave the bench honest on SIGTERM/SIGINT: LEDs dark, panel blank.

    Without this the process is killed outright — measured: no atexit, and the
    pin stays `op dh | hi`, so both LEDs stay lit and the SSD1306 keeps its last
    frame in GDDRAM. A frozen `REC 12:34` reads as "still recording", which is
    worse than stale: it is wrong.

    OUTPUTS ONLY. This must never touch .active, ffmpeg.pid, session.json or
    call end_session: ffmpeg is a separate process that reparents to init and
    KEEPS RECORDING across a daemon restart, and the restarted daemon relies on
    that on-disk state to resume (see _sync_recording_outputs at startup). Ending the
    session here would mark it ended while the capture kept growing, and
    removing the pidfile would make that capture unstoppable.

    Fail-soft: each device is cleared independently, LEDs first (register
    writes, cannot block) so a pathological panel stall still leaves the LEDs
    honest. Returning lets pause() return and the interpreter exit normally,
    which additionally runs gpiozero's and luma's own atexit cleanup.

    Still does NOT close an open mute span, and that stays correct: a
    shutdown-written span would assert the aside ended when the daemon stopped,
    which the operator never signalled. The span is instead persisted at
    MUTE-press time to <session>/mute_open.json and resumed by _recover_mute on
    the next start, so it survives a restart without anyone inventing an end.
    """
    global _stopping
    _stopping = True

    # 1. LEDs first: instant, cannot block. Wrapped separately so a failure on
    #    one still drops the other.
    #
    #    The RING LIGHT is deliberately absent from this list — this is not an
    #    oversight, do not "fix" it. The LEDs and the panel are INDICATORS: left
    #    lit with no daemon they lie about state, so clearing them is what makes
    #    the bench honest. The light indicates nothing; it illuminates. ffmpeg
    #    reparents to init and KEEPS RECORDING across a restart, so clearing the
    #    light here would burn a ~3s blackout (RestartSec) into the middle of a
    #    take and darken the operator's hands, and on a plain `stop` it would
    #    leave the bench dark while still recording. Leaving a light on is not a
    #    lie; it is just a light that is on. The next daemon start syncs it off
    #    if no session is running.
    for label, led in (("record", record_led), ("mute", status_led)):
        try:
            led.off()
        except Exception as e:  # noqa: BLE001 - keep clearing the rest
            print(f"[gpio] shutdown: {label} LED off failed — {e}")

    # 2. Panel: bounded, and behind the refresh thread's lock so a frame being
    #    flushed right now cannot repaint over the clear.
    if _oled is not None:
        acquired = _panel_lock.acquire(timeout=PANEL_CLEAR_TIMEOUT)
        try:
            if acquired:
                _oled.clear()  # blank GDDRAM...
                _oled.hide()   # ...then switch the panel off
            else:
                print("[gpio] shutdown: panel busy — left as-is (LEDs are off).")
        except Exception as e:  # noqa: BLE001 - never block the stop
            print(f"[gpio] shutdown: panel clear failed — {e}")
        finally:
            if acquired:
                _panel_lock.release()

    print(f"[gpio] shutdown on signal {signum} — clearing outputs.")


def _open_mute_span(session, elapsed, wall_iso):
    global _mute_open, _mute_session, _mute_start_seconds, _mute_start_wall, _mute_last_elapsed
    _mute_open = True
    _mute_session = session
    _mute_start_seconds = elapsed
    _mute_start_wall = wall_iso
    _mute_last_elapsed = elapsed
    # Persist BEFORE lighting the LED: once the operator sees the badge he is
    # entitled to assume the span survives a daemon restart.
    _persist_open_span(session, elapsed, wall_iso)
    status_led.on()


def _close_mute_span(end_seconds, end_wall_iso):
    """Write the open span's complete row and turn the LED off. Idempotent-ish:
    a no-op on state if nothing is open."""
    global _mute_open, _mute_session
    if _mute_session is not None:
        _append_mute_span(
            _mute_session, _mute_start_seconds, end_seconds,
            _mute_start_wall, end_wall_iso,
        )
        # The complete row is committed, so drop the in-flight record — a later
        # restart must not resurrect a span already in mute_spans.csv. This also
        # covers _reconcile_mute, which closes through here.
        _clear_open_span(_mute_session)
    _mute_open = False
    _mute_session = None
    status_led.off()


def _reconcile_mute(active):
    """Safety guard (no background poll): if a span is open but recording is no
    longer RUNNING, the LED would lie — close the span at the last-known elapsed
    and turn the LED off. Runs on the next interaction that loads the session."""
    if _mute_open and _running(active) is None:
        _close_mute_span(_mute_last_elapsed, clock.to_iso(clock.now()))
        print("[gpio] MUTE auto-closed — recording no longer running; LED off.")


def _recover_mute(active):
    """Resume (or discard) a mute span left open by a PREVIOUS daemon process.

    Runs once at startup against the same loaded `active` as _sync_recording_outputs.
    Record state survives a restart because it lives on disk; this gives an open
    mute span the same property via <session>/mute_open.json.

    Session still RUNNING -> resume muted. The operator pressed MUTE and never
    pressed it again, so the LED and OLED badge coming back on match what he
    believes. Silently unmuting would leave him talking into a live mic he
    thinks is muted — unrecoverable, where a lost span is not.

    Session already ENDED while the daemon was down -> discard. We never saw an
    unmute press and cannot know when the span should have ended; committing at
    ended_wall_time would fabricate a boundary we never observed.
    """
    global _mute_open, _mute_session, _mute_start_seconds, _mute_start_wall
    global _mute_last_elapsed
    if active is None:
        return
    path = _mute_open_path(active)
    if not path.exists():
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        start = float(data["start_seconds"])
        start_wall = str(data.get("start_wall_time", ""))
        owner = str(data.get("session_id", ""))
    except (OSError, ValueError, TypeError, KeyError) as e:  # noqa: BLE001
        print(f"[gpio] MUTE recover: {MUTE_OPEN_FILENAME} unreadable ({e}) — discarded.")
        _unlink_quiet(path)
        return

    if owner and owner != active.session_id:
        print(f"[gpio] MUTE recover: {MUTE_OPEN_FILENAME} belongs to {owner}, "
              f"not {active.session_id} — discarded.")
        _unlink_quiet(path)
        return

    if _running(active) is None:
        print(f"[gpio] MUTE recover: {active.session_id} already ended — open span "
              f"from {start:.2f}s discarded (no end was ever observed).")
        _unlink_quiet(path)
        return

    _mute_open = True
    _mute_session = active
    _mute_start_seconds = start
    _mute_start_wall = start_wall
    # CONSERVATIVE ON PURPOSE — do NOT "fix" this to elapsed_seconds(active) for
    # consistency with _open_mute_span. The daemon was down for part of this
    # interval and observed nothing, so a resumed span carries less evidence
    # than a normal one. Leaving it at the start means a later safety close
    # writes end == start: a zero-length row the editor skips, so the span is
    # LOST. That is the intended trade. Asserting coverage all the way to "now"
    # would risk force-lapsing narration the operator expected to be live,
    # silently deleting it from the published video. A lost span costs a manual
    # edit; a wrong span cannot be recovered.
    _mute_last_elapsed = start
    # Expected on a service restart: the SIGTERM handler clears this LED, then
    # this resume relights it ~RestartSec later. That brief dark-then-lit
    # flicker is correct behaviour, not a fault.
    status_led.on()
    print(f"[gpio] MUTE resumed from {start:.2f}s — span was left open by a "
          "previous daemon; press MUTE to close it. LED on.")


def on_start():
    try:
        active = _active_or_none()
        _reconcile_mute(active)  # clean up any stale span from a prior session
        _sync_recording_outputs(active)  # correct outputs even on the "ignored" path
        status_led.off()         # defensive: a new session never inherits mute
        if _running(active) is not None:
            print("[gpio] START ignored — already recording."); return
        s = create_session(ROOT, recorder=RECORDER)
        get_recorder(s.recorder).start(s.folder)
        start_session(s)
        # Only now is a session RUNNING: create_session/recorder.start above can
        # raise, and then no transition happened and both must stay off.
        record_led.on()
        ring_light.on()
        print(f"[gpio] START — recording {s.session_id}")
    except Exception as e:
        print(f"[gpio] START failed — {e}")


def on_mark():
    global _mute_last_elapsed
    try:
        active = _active_or_none()
        _reconcile_mute(active)
        _sync_recording_outputs(active)
        s = _running(active)
        if s is None:
            print("[gpio] MARK ignored — nothing recording."); return
        m = add_marker(s, "", source="gpio")
        if _mute_open:
            _mute_last_elapsed = m.elapsed_seconds  # keep the safety value fresh
        print(f"[gpio] MARK #{m.marker_index} @ {m.elapsed_seconds:.2f}s")
    except Exception as e:
        print(f"[gpio] MARK failed — {e}")


def on_mute():
    global _mute_last_elapsed
    try:
        active = _active_or_none()
        _reconcile_mute(active)
        _sync_recording_outputs(active)
        s = _running(active)
        if s is None:
            print("[gpio] MUTE ignored — nothing recording."); return
        elapsed = elapsed_seconds(s)
        wall_iso = clock.to_iso(clock.now())
        _mute_last_elapsed = elapsed
        if not _mute_open:
            _open_mute_span(s, elapsed, wall_iso)
            print(f"[gpio] MUTE on  @ {elapsed:.2f}s — LED on")
        else:
            _close_mute_span(elapsed, wall_iso)
            print(f"[gpio] MUTE off @ {elapsed:.2f}s — LED off")
    except Exception as e:
        print(f"[gpio] MUTE failed — {e}")


def on_stop():
    try:
        active = _active_or_none()
        s = _running(active)
        if s is None:
            _reconcile_mute(active)  # nothing to stop, but don't let the LED lie
            _sync_recording_outputs(active)  # ...and neither should the LED or light
            print("[gpio] STOP ignored — nothing recording."); return
        # Close any open mute span at end-of-recording BEFORE ending the session,
        # so there is never a dangling open span and the LED goes off.
        if _mute_open:
            _close_mute_span(elapsed_seconds(s), clock.to_iso(clock.now()))
            print("[gpio] MUTE closed at STOP — LED off")
        get_recorder(s.recorder).stop(s.folder)
        end_session(s)
        # Only after end_session: recorder.stop() can RAISE (wedged ffmpeg), and
        # then the session is still RUNNING and the capture may still be writing —
        # the LED must stay ON rather than lie about it, and the light must stay
        # ON rather than darken a bench that is still being recorded (and where
        # the operator is now troubleshooting a failed stop by hand).
        record_led.off()
        ring_light.off()
        print(f"[gpio] STOP — ended {s.session_id}")
    except Exception as e:
        print(f"[gpio] STOP failed — {e}")


# Keep references to the Button/LED objects, or gpiozero garbage-collects them
# and the callbacks stop firing (and the LED would drop off).
start_btn = Button(27, pull_up=True, bounce_time=0.05)
mark_btn  = Button(17, pull_up=True, bounce_time=0.05)
stop_btn  = Button(22, pull_up=True, bounce_time=0.05)
mute_btn  = Button(23, pull_up=True, bounce_time=0.05)
status_led = LED(24)
# ACTIVE-HIGH to match the as-built v1 control PCB: D1 is hardwired
# GPIO5 -> R1(330R) -> anode, cathode -> GND. Do NOT add active_high=False — it
# would invert D1 once the ribbon moves to the board.
# v2 item: GPIO5 defaults to an internal pull-UP (BCM 0-8 do; 9-27 pull down),
# so the pin floats high at boot and during the ~3s systemd RestartSec window —
# roughly 30uA through D1, a very faint glow before the daemon claims the pin as
# an output. Accepted property of the v1 hardware; not worked around in code.
record_led = LED(5)
# Bench ring light, switched by an IRLZ44N low-side N-channel MOSFET: GPIO6 ->
# gate, 10k gate pulldown to GND (holds the gate low through boot, so the light
# is off before this daemon claims the pin — measured: GPIO6 idles `ip pn | lo`,
# no internal pull fighting the pulldown). Active-high is correct here: pin high
# -> gate high -> FET conducts -> light on. initial_value=False (the default)
# drives the gate low at construction, so claiming the pin never flashes it.
#
# DigitalOutputDevice, NOT LED, on purpose: this is a power-switching output, not
# an indicator. `LED` in this file means "indicator" (record_led/status_led), and
# a third LED(...) would invite someone to sweep all LEDs off in _shutdown —
# exactly the bug that would black out an ongoing take.
#
# LOAD STATUS: currently a STAND-IN LED on the breadboard. The real ring light is
# NOT yet validated — sub-1A at 5V through USB pigtails, pending a meter check of
# light-ground-to-Pi-ground continuity through the hub.
#
# MANUAL OVERRIDE: an SS12D00G is earmarked but NOT wired and NOT on the milled
# board — treat it as unbuilt; nothing here depends on it. When it is built, wire
# it in PARALLEL across drain-source (force-ON only). A series switch could
# defeat the daemon, which would make the light's real state unknowable to this
# process and would then require a light-state indicator on the OLED.
ring_light = DigitalOutputDevice(6)
start_btn.when_pressed = on_start
mark_btn.when_pressed  = on_mark
stop_btn.when_pressed  = on_stop
mute_btn.when_pressed  = on_mute
status_led.off()  # start dark; _recover_mute relights it only if a span is open
# Every output is DERIVED from disk, so a daemon restart mid-session shows the
# truth rather than a default. Recording state survives because ffmpeg reparents
# to init and keeps running; an open mute span survives via the daemon-private
# <session>/mute_open.json written at MUTE-press time. One loaded `active` feeds
# all of them so they cannot disagree. This is also what keeps a restart from
# turning into a blackout: the ring light comes straight back ON here if the
# session is still RUNNING.
_active_at_start = _active_or_none()
_sync_recording_outputs(_active_at_start)
_recover_mute(_active_at_start)

# OLED last: it is the only optional peripheral, and it must never hold up the
# buttons. A missing/unplugged panel logs one line and the daemon runs on.
_oled = _init_oled()
_oled_last_attempt = time.monotonic()
if _oled is None:
    _oled_failing = True
    reason = "luma/PIL not importable" if not _OLED_IMPORTS_OK else "no response"
    print(f"[gpio] OLED not found at i2c-{OLED_PORT}@0x{OLED_ADDRESS:02x} "
          f"({reason}) — display off; capture unaffected. Retrying every "
          f"{OLED_RETRY_SECONDS:g}s.")
# Daemon thread: dies with the process, needs no shutdown handling. The buttons
# stay event-driven — gpiozero dispatches their callbacks on its own threads, so
# this tick (and a wedged I2C write) can never delay a press.
threading.Thread(target=_display_loop, name="oled", daemon=True).start()

# Registered LAST, once every output device exists (the handler dereferences all
# three). With a Python-level handler installed, pause() RETURNS instead of the
# process being killed, so the interpreter shuts down normally and gpiozero's +
# luma's own atexit hooks also run. SIGINT gets the same treatment so Ctrl+C on
# a manually-run daemon behaves identically (and no longer prints a traceback).
signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)

print(f"[gpio] BenchCam button daemon ready. sessions root: {ROOT}")
print("[gpio] START=27  MARK=17  STOP=22  MUTE=23  MUTE-LED=24  REC-LED=5  "
      f"LIGHT=6  OLED=i2c-{OLED_PORT}@0x{OLED_ADDRESS:02x}.  Ctrl+C to quit.")
pause()  # returns once _shutdown has run; falls through to a clean exit 0
print("[gpio] stopped — LEDs off, panel cleared. Any capture keeps running.")
