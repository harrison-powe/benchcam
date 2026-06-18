"""Local web dashboard for running a whole BenchCam session without a terminal.

``benchcam dashboard`` starts a tiny stdlib ``http.server`` bound to localhost
and opens the default browser to a single-page UI. The page calls small JSON
endpoints that drive the EXISTING BenchCam logic — it does not reimplement
sessions, markers, recorders, collect, or edit:

    start   -> session.create_session + recorder.start + session.start_session
    mark    -> session.add_marker (instant; label optional)
    label   -> markers.set_marker_label (label/relabel an existing marker)
    note    -> append a line to notes.md
    stop    -> recorder.stop (collect happens inside the OBS recorder) +
               session.end_session
    review  -> editor.run_edit

The dashboard is a launch/start/stop/review + click-to-mark convenience. For
fast hands-busy marking, ``benchcam live`` in a terminal is still the quickest
path; the dashboard does not replace it.

Local-only (127.0.0.1), no auth, stdlib only (http.server, json, webbrowser).
The OBS client stays the isolated optional extra (imported only when the OBS
recorder is actually used).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import clock
from . import config as config_mod
from . import editor as editor_mod
from . import session as session_mod
from .editor import EditError
from .markers import read_markers, set_marker_label
from .recorders import get_recorder
from .recorders.base import RecorderError
from .recorders.obs import ENV_HOST, ENV_PASSWORD, ENV_PORT
from .session import SessionError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
RECORDER_CHOICES = ("obs", "ffmpeg", "null")


class DashboardError(RuntimeError):
    """Raised for dashboard-level state problems (e.g. double start)."""


class DashboardController:
    """Holds the in-memory session + recorder and drives the existing logic.

    The recorder instance must persist between start and stop (the OBS websocket
    connection / ffmpeg subprocess handle), so the dashboard keeps it in memory
    just like the ``live`` shell keeps the session in memory.
    """

    def __init__(
        self,
        sessions_root: Path | str,
        config_root: Path | str | None = None,
    ) -> None:
        self.sessions_root = Path(sessions_root)
        # Where .benchcam/config.json lives. Defaults to a STABLE, cwd-independent
        # location so a saved OBS password is reused on every launch (Fix 1).
        self._config_root = (
            Path(config_root)
            if config_root is not None
            else config_mod.default_config_root()
        )
        self._session: session_mod.Session | None = None
        self._recorder = None
        self._last_session: session_mod.Session | None = None
        self._last_review: str | None = None

    # -- queries -------------------------------------------------------------
    def get_config(self) -> dict:
        """Return non-secret OBS settings for prefilling the UI.

        The password is never returned — only whether one is saved.
        """
        obs = config_mod.load_config(self._config_root).get("obs", {})
        return {
            "config": {
                "obs": {
                    "host": obs.get("host") or "",
                    "port": obs.get("port") or "",
                    "has_password": bool(obs.get("password")),
                }
            }
        }

    def status(self) -> dict:
        if self._session is None:
            return {
                "active": False,
                "marker_count": 0,
                "markers": [],
                "elapsed_seconds": 0.0,
                "last_session": self._summary(self._last_session),
                "last_review": self._last_review,
            }
        session = self._session
        markers = self._markers(session)
        return {
            "active": True,
            "session_id": session.session_id,
            "folder": str(session.folder),
            "recorder": session.recorder,
            "elapsed_seconds": self._elapsed(session),
            "marker_count": len(markers),
            "markers": markers,
            "last_review": self._last_review,
        }

    # -- actions -------------------------------------------------------------
    def start(
        self,
        recorder_name: str = "obs",
        profile: str = "default",
        *,
        obs_password: str | None = None,
        obs_host: str | None = None,
        obs_port=None,
    ) -> dict:
        if self._session is not None:
            raise DashboardError(
                f"A session ({self._session.session_id}) is already active. "
                "Stop it before starting another."
            )
        recorder_name = (recorder_name or "obs").strip().lower()
        profile = (profile or "default").strip() or "default"

        if recorder_name == "obs":
            # Resolve + persist the OBS connection settings so the (unchanged)
            # ObsRecorder picks them up via its normal env resolution.
            self._prepare_obs_connection(obs_password, obs_host, obs_port)

        recorder = get_recorder(recorder_name)
        session = session_mod.create_session(
            root=self.sessions_root, profile=profile, recorder=recorder_name
        )
        # recorder.start may raise (e.g. OBS not running) — surface it loudly and
        # do not leave a half-started session marked active.
        recorder.start(session.folder)
        session_mod.start_session(session)

        self._session = session
        self._recorder = recorder
        self._last_review = None
        return self.status()

    def mark(self, label: str = "") -> dict:
        session = self._require_active()
        marker = session_mod.add_marker(session, label or "", source="manual")
        return {
            "marker": {
                "index": marker.marker_index,
                "elapsed": marker.elapsed_seconds,
                "label": marker.label,
            },
            **self.status(),
        }

    def note(self, text: str) -> dict:
        session = self._require_active()
        line = (text or "").rstrip("\n")
        with session.notes_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return {"note_added": line}

    def stop(self) -> dict:
        if self._session is None:
            return {
                "active": False,
                "stopped": False,
                "message": "No active session to stop.",
            }
        session = self._session
        recorder = self._recorder

        # Stop the recorder (for OBS this sends StopRecord and collects the video
        # into the session folder). If it fails — e.g. OBS was already stopped
        # manually — record the warning but still end the session cleanly so the
        # UI never stays stuck "RECORDING".
        warning = None
        try:
            if recorder is not None:
                recorder.stop()
        except Exception as exc:  # noqa: BLE001 - never block the end transition
            warning = f"Recorder stop reported: {exc}"

        try:
            session_mod.end_session(session)  # status -> ended, stamps ended time
        except SessionError:
            pass  # already ended; still fine

        self._session = None
        self._recorder = None
        self._last_session = session

        result = {
            "active": False,
            "stopped": True,
            "summary": self._summary(session),
        }
        if warning:
            result["warning"] = warning
        return result

    def label_marker(self, index, label: str = "") -> dict:
        """Set/edit the label of an existing marker (mark first, label after)."""
        session = self._session or self._last_session
        if session is None:
            raise DashboardError("No session with markers to label.")
        try:
            marker_index = int(index)
        except (TypeError, ValueError) as exc:
            raise DashboardError(f"Invalid marker index {index!r}.") from exc
        if not set_marker_label(session.markers_file, marker_index, label or ""):
            raise DashboardError(f"Marker #{marker_index} not found.")
        if self._session is not None:
            return {"labeled": marker_index, **self.status()}
        return {"labeled": marker_index}

    def review(
        self,
        pre: float = editor_mod.DEFAULT_PRE,
        post: float = editor_mod.DEFAULT_POST,
        speed: float = editor_mod.DEFAULT_SPEED,
    ) -> dict:
        target = self._last_session or self._session
        if target is None:
            raise DashboardError(
                "No finished session to review yet. Stop a session first."
            )
        output = editor_mod.run_edit(
            target.folder, pre=pre, post=post, speed=speed, out=lambda _m: None
        )
        self._last_review = str(output)
        # Auto-open the clip in the system default player (best-effort). The
        # server runs on the user's machine, so this pops the video open for
        # them; failure is non-fatal and we still return the link.
        opened = True
        try:
            open_file(output)
        except Exception:  # noqa: BLE001 - never fail review on an open() problem
            opened = False
        return {"review_path": str(output), "opened": opened}

    # -- helpers -------------------------------------------------------------
    def _prepare_obs_connection(self, password, host, port) -> None:
        """Persist any explicitly-provided OBS settings, then resolve them.

        Resolution order is explicit field -> saved config -> the existing
        BENCHCAM_OBS_* env vars -> ObsRecorder defaults (and finally ObsRecorder's
        own clear error if no password is available). Field/config values are
        applied to this process's env so the unchanged ObsRecorder reads them; an
        absent field/config leaves any real env var untouched.
        """
        cfg = config_mod.load_config(self._config_root)
        obs = dict(cfg.get("obs", {}))

        changed = False
        if password:
            obs["password"] = password
            changed = True
        if host:
            obs["host"] = host
            changed = True
        if port not in (None, ""):
            try:
                obs["port"] = int(port)
            except (TypeError, ValueError):
                pass
            else:
                changed = True
        if changed:
            cfg["obs"] = obs
            config_mod.save_config(cfg, self._config_root)

        # Apply field-or-config values to the env (only when present, so a real
        # BENCHCAM_OBS_PASSWORD still works when nothing is saved).
        resolved_password = password or obs.get("password")
        resolved_host = host or obs.get("host")
        resolved_port = port or obs.get("port")
        if resolved_password:
            os.environ[ENV_PASSWORD] = str(resolved_password)
        if resolved_host:
            os.environ[ENV_HOST] = str(resolved_host)
        if resolved_port:
            os.environ[ENV_PORT] = str(resolved_port)

    def _require_active(self) -> session_mod.Session:
        if self._session is None:
            raise DashboardError(
                "No active session. Start one before marking or adding notes."
            )
        return self._session

    @staticmethod
    def _markers(session: session_mod.Session) -> list[dict]:
        markers = []
        for row in read_markers(session.markers_file):
            try:
                markers.append(
                    {
                        "index": int(row["marker_index"]),
                        "elapsed": float(row["elapsed_seconds"]),
                        "label": row.get("label", ""),
                    }
                )
            except (KeyError, ValueError):
                continue
        return markers

    @staticmethod
    def _elapsed(session: session_mod.Session) -> float:
        baseline = session.started_wall_time or session.created_wall_time
        try:
            return max((clock.now() - clock.from_iso(baseline)).total_seconds(), 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _summary(self, session: session_mod.Session | None) -> dict | None:
        if session is None:
            return None
        duration = 0.0
        if session.started_wall_time and session.ended_wall_time:
            try:
                duration = max(
                    (
                        clock.from_iso(session.ended_wall_time)
                        - clock.from_iso(session.started_wall_time)
                    ).total_seconds(),
                    0.0,
                )
            except (TypeError, ValueError):
                duration = 0.0
        return {
            "session_id": session.session_id,
            "folder": str(session.folder),
            "recorder": session.recorder,
            "marker_count": len(self._markers(session)),
            "duration_seconds": duration,
        }


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "BenchCamDashboard/1.0"

    def log_message(self, *args) -> None:  # keep the console quiet
        return

    @property
    def controller(self) -> DashboardController:
        return self.server.controller  # type: ignore[attr-defined]

    def _send_json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_html(PAGE_HTML)
        elif path == "/api/status":
            self._guard(lambda _: self.controller.status(), {})
        elif path == "/api/config":
            self._guard(lambda _: self.controller.get_config(), {})
        else:
            self._send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        payload = self._read_json()
        routes = {
            "/api/start": lambda p: self.controller.start(
                p.get("recorder", "obs"),
                p.get("profile", "default"),
                obs_password=p.get("obs_password") or None,
                obs_host=p.get("obs_host") or None,
                obs_port=p.get("obs_port") or None,
            ),
            "/api/mark": lambda p: self.controller.mark(p.get("label", "")),
            "/api/label": lambda p: self.controller.label_marker(
                p.get("index"), p.get("label", "")
            ),
            "/api/note": lambda p: self.controller.note(p.get("text", "")),
            "/api/stop": lambda p: self.controller.stop(),
            "/api/review": lambda p: self.controller.review(
                pre=_as_float(p.get("pre"), editor_mod.DEFAULT_PRE),
                post=_as_float(p.get("post"), editor_mod.DEFAULT_POST),
                speed=_as_float(p.get("speed"), editor_mod.DEFAULT_SPEED),
            ),
        }
        handler = routes.get(path)
        if handler is None:
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        self._guard(handler, payload)

    def _guard(self, action, payload) -> None:
        """Run an action, returning {ok:True, ...} or {ok:False, error:...}."""
        try:
            result = action(payload)
            self._send_json({"ok": True, **result})
        except (DashboardError, SessionError, RecorderError, EditError) as exc:
            self._send_json({"ok": False, "error": str(exc)})
        except Exception as exc:  # never crash the dashboard on a bad request
            self._send_json({"ok": False, "error": f"unexpected error: {exc}"})


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def open_file(path) -> None:
    """Open a file in the OS default application (Windows/macOS/Linux).

    Used to pop ``review.mp4`` open in the default media player when an edit
    finishes. Best-effort; callers treat any failure as non-fatal.
    """
    target = str(path)
    if sys.platform.startswith("win"):
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: PLW1514 - Windows only
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


def make_server(host: str, port: int, sessions_root: Path | str) -> tuple[HTTPServer, DashboardController]:
    """Create the HTTP server + controller (used by serve() and by tests)."""
    controller = DashboardController(sessions_root)
    httpd = HTTPServer((host, port), DashboardHandler)
    httpd.controller = controller  # type: ignore[attr-defined]
    return httpd, controller


def serve(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    sessions_root: Path | str,
    open_browser: bool = True,
) -> int:
    try:
        httpd, _ = make_server(host, port, sessions_root)
    except OSError as exc:
        raise DashboardError(
            f"Could not start the dashboard on {host}:{port}: {exc}. "
            "Is another dashboard already running on that port?"
        ) from exc

    url = f"http://{host}:{port}/"
    print(f"BenchCam dashboard running at {url}")
    print("Keep this window open. Close it (or press Ctrl+C) to stop the dashboard.")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------- #
# Single-page UI (no external assets)
# --------------------------------------------------------------------------- #

PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BenchCam Dashboard</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         margin: 0; padding: 0 16px 48px; background: #14161a; color: #e7e9ee; }
  .wrap { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 20px; font-weight: 600; margin: 18px 0 4px; }
  .sub { color: #9aa3af; font-size: 13px; margin-bottom: 16px; }
  .state { display: flex; align-items: center; gap: 12px; padding: 16px 18px;
           border-radius: 12px; font-size: 20px; font-weight: 700; margin-bottom: 18px;
           border: 1px solid #2a2f37; }
  .state .dot { width: 16px; height: 16px; border-radius: 50%; }
  .state.idle { background: #1b1e24; color: #9aa3af; }
  .state.idle .dot { background: #6b7280; }
  .state.rec { background: #2a1416; color: #ff6b6b; }
  .state.rec .dot { background: #ff3b3b; box-shadow: 0 0 0 0 rgba(255,59,59,.7);
                    animation: pulse 1.4s infinite; }
  @keyframes pulse { 0%{box-shadow:0 0 0 0 rgba(255,59,59,.6);}
                     70%{box-shadow:0 0 0 12px rgba(255,59,59,0);}
                     100%{box-shadow:0 0 0 0 rgba(255,59,59,0);} }
  .card { background: #1b1e24; border: 1px solid #2a2f37; border-radius: 12px;
          padding: 16px; margin-bottom: 16px; }
  .card h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .05em;
             color: #9aa3af; margin: 0 0 12px; }
  label { font-size: 13px; color: #c4c9d2; display: block; margin: 8px 0 4px; }
  input, select { background: #0f1115; color: #e7e9ee; border: 1px solid #333a44;
                  border-radius: 8px; padding: 9px 10px; font-size: 14px; width: 100%; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  .row > * { flex: 1; min-width: 120px; }
  button { cursor: pointer; border: none; border-radius: 8px; padding: 11px 14px;
           font-size: 14px; font-weight: 600; color: #fff; background: #3b82f6; }
  button.big { padding: 16px; font-size: 18px; width: 100%; }
  button.mark { background: #16a34a; }
  button.stop { background: #dc2626; }
  button.ghost { background: #334155; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .mut { color: #9aa3af; font-size: 13px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #262b33; }
  th { color: #9aa3af; font-weight: 600; }
  .err { background: #2a1416; border: 1px solid #5b1f22; color: #ff8a8a;
         padding: 10px 12px; border-radius: 8px; margin-bottom: 14px; display: none; }
  .ok { color: #34d399; }
  code { background: #0f1115; padding: 1px 6px; border-radius: 5px; }
  .hidden { display: none; }
  .hint { font-size: 12px; color: #6b7280; margin-top: 6px; }
  .confirm { margin-top: 12px; padding: 12px 14px; border-radius: 10px;
             background: #2a1416; border: 1px solid #5b1f22; color: #ffb4b4; }
  .confirm b { color: #fff; }
  .confirm .actions { margin-top: 10px; display: flex; gap: 10px; }
  .confirm .actions button { flex: 0 0 auto; }
</style>
</head>
<body>
<div class="wrap">
  <h1>BenchCam</h1>
  <div class="sub">Local bench-session dashboard &middot; start &rarr; mark &rarr; stop &rarr; review</div>

  <div id="state" class="state idle"><span class="dot"></span><span id="stateText">IDLE</span></div>
  <div id="error" class="err"></div>

  <!-- START -->
  <div id="startCard" class="card">
    <h2>Start a session</h2>
    <div class="row">
      <div>
        <label for="recorder">Recorder</label>
        <select id="recorder">
          <option value="obs" selected>OBS (camera preview in OBS)</option>
          <option value="ffmpeg">ffmpeg (webcam direct)</option>
          <option value="null">null (markers only)</option>
        </select>
      </div>
      <div>
        <label for="profile">Profile (optional)</label>
        <input id="profile" placeholder="default" />
      </div>
    </div>
    <div id="obsPanel">
      <p class="hint">OBS recorder needs OBS Studio running with its WebSocket server enabled
         (Tools &rarr; WebSocket Server Settings).</p>
      <div class="row">
        <div style="flex:2">
          <label for="obsPassword">OBS WebSocket password</label>
          <input id="obsPassword" type="password" placeholder="enter password (saved after first use)" autocomplete="off" />
        </div>
        <div><label for="obsHost">Host</label><input id="obsHost" placeholder="localhost" /></div>
        <div><label for="obsPort">Port</label><input id="obsPort" placeholder="4455" /></div>
      </div>
      <p id="obsSaved" class="hint"></p>
    </div>
    <div style="margin-top:12px"><button id="startBtn" class="big">Start session</button></div>
  </div>

  <!-- ACTIVE -->
  <div id="activeCard" class="card hidden">
    <h2>Recording</h2>
    <p class="mut">Session <code id="sid"></code> &middot; recorder <code id="rec"></code><br>
       elapsed <b id="elapsed">0.0s</b> &middot; <b id="count">0</b> marker(s)
       &middot; <code id="folder"></code></p>
    <div style="margin:12px 0"><button id="markBtn" class="big mark">MARK now</button></div>
    <div class="row">
      <div style="flex:3"><input id="label" placeholder="label (optional)" /></div>
      <div style="flex:1"><button id="markLabelBtn">Mark + label</button></div>
    </div>
    <div class="row" style="margin-top:10px">
      <div style="flex:3"><input id="note" placeholder="add a note to notes.md" /></div>
      <div style="flex:1"><button id="noteBtn" class="ghost">Add note</button></div>
    </div>
    <p class="hint">Tip: just hit <b>MARK now</b> (or <b>Space</b>) the instant something happens —
       you can type the label onto that row afterward, in the Markers list below.</p>
    <p class="hint" id="legend">Keyboard (when not typing in a field):
       <b>Space</b>/<b>Enter</b> = mark now &middot;
       <b>L</b> = label the last marker &middot;
       <b>N</b> = add note &middot;
       <b>Q</b> = stop session.
       For hands-busy marking, <code>benchcam live</code> in a terminal is still fastest.</p>
    <div style="margin-top:14px"><button id="stopBtn" class="big stop">Stop session</button></div>
    <div id="stopConfirm" class="confirm hidden">
      Stop session? Press <b>Q</b> again to confirm, <b>Esc</b> to cancel.
      <div class="actions">
        <button id="stopConfirmYes" class="stop">Confirm stop</button>
        <button id="stopConfirmNo" class="ghost">Cancel</button>
      </div>
    </div>
  </div>

  <!-- MARKERS -->
  <div id="markersCard" class="card hidden">
    <h2>Markers</h2>
    <table><thead><tr><th>#</th><th>elapsed</th><th>label</th></tr></thead>
    <tbody id="markers"></tbody></table>
  </div>

  <!-- REVIEW -->
  <div id="reviewCard" class="card hidden">
    <h2>Last session &amp; review</h2>
    <p id="summary" class="mut"></p>
    <div class="row">
      <div><label>pre (s)</label><input id="pre" type="number" value="3" step="0.5"></div>
      <div><label>post (s)</label><input id="post" type="number" value="5" step="0.5"></div>
      <div><label>speed (x)</label><input id="speed" type="number" value="8" step="1"></div>
    </div>
    <div style="margin-top:12px"><button id="reviewBtn" class="big">Make review.mp4</button></div>
    <p id="reviewOut" class="ok"></p>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let busy = false;

function showError(msg) {
  const e = $("error");
  if (msg) { e.textContent = msg; e.style.display = "block"; }
  else { e.style.display = "none"; }
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: {"Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (!data.ok && data.error) showError(data.error); else showError(null);
  return data;
}

let current = {active: false};
let renderedCount = -1;

function render(s) {
  current = s;
  const active = s.active;
  $("state").className = "state " + (active ? "rec" : "idle");
  $("stateText").textContent = active ? "● RECORDING" : "○ IDLE";
  $("startCard").classList.toggle("hidden", active);
  $("activeCard").classList.toggle("hidden", !active);
  $("markersCard").classList.toggle("hidden", !active || (s.markers||[]).length === 0);
  if (active) {
    $("sid").textContent = s.session_id;
    $("rec").textContent = s.recorder;
    $("folder").textContent = s.folder;
    $("elapsed").textContent = (s.elapsed_seconds||0).toFixed(1) + "s";
    $("count").textContent = s.marker_count||0;
    renderMarkers(s.markers||[]);
  } else {
    renderedCount = -1;  // start the next session's table fresh
    if (typeof hideStopConfirm === "function") hideStopConfirm();
  }
  // review card: show when there is a finished session
  const last = s.last_session;
  $("reviewCard").classList.toggle("hidden", active || !last);
  if (!active && last) {
    $("summary").innerHTML = `Session <code>${last.session_id}</code> — `
      + `${last.marker_count} marker(s), ${last.duration_seconds.toFixed(1)}s — `
      + `<code>${last.folder}</code>`;
  }
  if (s.last_review) $("reviewOut").textContent = "review.mp4: " + s.last_review;
}

// Rebuild the markers table only when the count changes, so the 1s status poll
// never wipes a label you're mid-typing. Focus/caret are preserved across the
// occasional rebuild.
function renderMarkers(markers) {
  if (markers.length === renderedCount) return;
  const tb = $("markers");
  const act = document.activeElement;
  const focusIdx = (act && act.classList && act.classList.contains("lbl")) ? act.dataset.index : null;
  const caret = (focusIdx != null) ? act.selectionStart : null;
  renderedCount = markers.length;
  tb.innerHTML = "";
  markers.slice().reverse().forEach(m => {
    const tr = document.createElement("tr");
    const tdN = document.createElement("td"); tdN.textContent = m.index;
    const tdE = document.createElement("td"); tdE.textContent = m.elapsed.toFixed(2) + "s";
    const tdL = document.createElement("td");
    const inp = document.createElement("input");
    inp.className = "lbl"; inp.dataset.index = m.index;
    inp.value = m.label || ""; inp.placeholder = "add label";
    inp.addEventListener("keydown", ev => {
      if (ev.key === "Enter") { ev.preventDefault(); saveLabel(m.index, inp.value); inp.blur(); }
    });
    inp.addEventListener("change", () => saveLabel(m.index, inp.value));
    tdL.appendChild(inp);
    tr.appendChild(tdN); tr.appendChild(tdE); tr.appendChild(tdL);
    tb.appendChild(tr);
  });
  if (focusIdx != null) {
    const sel = tb.querySelector('input.lbl[data-index="' + focusIdx + '"]');
    if (sel) { sel.focus(); if (caret != null) { try { sel.setSelectionRange(caret, caret); } catch(e){} } }
  }
}

async function saveLabel(index, value) {
  const d = await api("/api/label", {index: index, label: value});
  if (d.ok && d.marker_count !== undefined) { current = d; $("count").textContent = d.marker_count; }
}

async function doMark() { const d = await api("/api/mark", {label: ""}); if (d.ok) render(d); }

function focusLastLabel() {
  const first = $("markers").querySelector("input.lbl");  // reversed => most recent first
  if (first) { first.focus(); first.select(); }
}

function escapeHtml(t){return t.replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}

async function refresh(){ try { render(await api("/api/status")); } catch(e){} }

function toggleObsPanel(){ $("obsPanel").style.display = ($("recorder").value === "obs") ? "block" : "none"; }
$("recorder").onchange = toggleObsPanel;

async function loadConfig(){
  try {
    const d = await api("/api/config");
    if (d.ok && d.config && d.config.obs){
      const o = d.config.obs;
      if (o.host) $("obsHost").value = o.host;
      if (o.port) $("obsPort").value = o.port;
      if (o.has_password) {
        $("obsPassword").placeholder = "saved — leave blank to reuse";
        $("obsSaved").textContent = "A password is saved locally — leave the field blank to reuse it.";
      } else {
        $("obsPassword").placeholder = "enter password (saved after first use)";
        $("obsSaved").textContent = "No password saved yet — enter it once and it will be remembered.";
      }
    }
  } catch(e){}
  toggleObsPanel();
}

$("startBtn").onclick = async () => {
  $("startBtn").disabled = true;
  const body = {recorder: $("recorder").value, profile: $("profile").value};
  if ($("recorder").value === "obs"){
    body.obs_password = $("obsPassword").value;
    body.obs_host = $("obsHost").value;
    body.obs_port = $("obsPort").value;
  }
  const d = await api("/api/start", body);
  $("startBtn").disabled = false;
  if (d.ok) { $("obsPassword").value = ""; render(d); }
};
$("markBtn").onclick = doMark;
$("markLabelBtn").onclick = async () => {
  const d = await api("/api/mark", {label: $("label").value}); if (d.ok) { $("label").value=""; render(d); }
};
// Enter submits AND exits the field (blurs) so Space/L/N/Q shortcuts work again
// immediately — same behavior for the label field and the note field (Fix 2).
$("label").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); $("markLabelBtn").click(); e.target.blur(); }
});
$("noteBtn").onclick = async () => {
  const d = await api("/api/note", {text: $("note").value}); if (d.ok) $("note").value="";
};
$("note").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); $("noteBtn").click(); e.target.blur(); }
});

// Keyboard shortcuts — only when a session is active and you're NOT typing in a
// field, so spacebar-mark never fires while you're entering a label or note.
document.addEventListener("keydown", (e) => {
  const t = e.target;
  const tag = ((t && t.tagName) || "").toLowerCase();
  const typing = tag === "input" || tag === "textarea" || tag === "select" || (t && t.isContentEditable);
  if (typing) return;
  if (!current.active) return;
  const k = e.key;
  if (k === "Escape") { if (confirmingStop) { e.preventDefault(); hideStopConfirm(); } return; }
  if (k === " " || k === "Spacebar" || k === "Enter") { e.preventDefault(); doMark(); }
  else if (k === "l" || k === "L") { e.preventDefault(); focusLastLabel(); }
  else if (k === "n" || k === "N") { e.preventDefault(); $("note").focus(); }
  else if (k === "q" || k === "Q") { e.preventDefault(); requestStop(); }
});

// In-page stop confirmation — fully keyboard-driven, no native browser dialog.
let confirmingStop = false;
function showStopConfirm() { confirmingStop = true; $("stopConfirm").classList.remove("hidden"); }
function hideStopConfirm() { confirmingStop = false; $("stopConfirm").classList.add("hidden"); }
function requestStop() { if (confirmingStop) doStop(); else showStopConfirm(); }
async function doStop() {
  hideStopConfirm();
  $("stopBtn").disabled = true;
  const d = await api("/api/stop", {});   // POST (a GET would 404 and never stop)
  $("stopBtn").disabled = false;
  if (d.ok && d.warning) showError(d.warning);  // ended cleanly, but note the issue
  await refresh();
}
$("stopBtn").onclick = requestStop;       // mouse click shows the same in-page confirm
$("stopConfirmYes").onclick = doStop;
$("stopConfirmNo").onclick = hideStopConfirm;
$("reviewBtn").onclick = async () => {
  $("reviewBtn").disabled = true;
  $("reviewOut").textContent = "Rendering review.mp4 (this can take a while)…";
  const d = await api("/api/review", {pre: $("pre").value, post: $("post").value, speed: $("speed").value});
  $("reviewBtn").disabled = false;
  if (d.ok) $("reviewOut").textContent = "review.mp4: " + d.review_path;
  else $("reviewOut").textContent = "";
};

loadConfig();
refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""
