"""Laptop-side review dashboard for BenchCam sessions captured on the Pi.

The normal ``benchcam dashboard`` runs ON the Pi (the capture box). Because it
is served from a headless Pi, its "Open folder" / "Open video" / "Make review"
buttons can't do anything useful — a web page served by the Pi can't reach the
laptop's screen or file explorer, and the heavy H.264 render shouldn't run on a
2GB Pi anyway.

This file is the missing other half: a tiny localhost-only dashboard you run ON
THE LAPTOP. It lists the sessions sitting on the Pi (over ``ssh ls``) and gives
working buttons that:

  * Open folder  -> copy the session here (once) and open it in Explorer
  * Watch        -> copy the session here (once) and play capture.mkv in VLC
  * Make review  -> copy the session here (once), render review.mp4, play it

It does NOT reimplement the copy or the render. It shells out to the existing
CLI you already have:

    copy:    python -m benchcam fetch <id> --no-open --sessions-root <ABS_ROOT>
    render:  python -m benchcam edit --session <id>  --sessions-root <ABS_ROOT>

Standard library only (http.server / json / subprocess / pathlib / os / shutil),
mirroring src/benchcam/dashboard.py. Bound to 127.0.0.1 only — no auth, never
exposed on the LAN.

Run it:  python reviewer.py   (then open http://127.0.0.1:8770/)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config (resolved once at startup; printed on launch)
# --------------------------------------------------------------------------- #

#: Where fetched sessions land on the laptop. Resolved to ABSOLUTE so it is
#: independent of the cwd the shelled-out CLI happens to inherit.
LOCAL_ROOT = Path(os.environ.get("BENCHCAM_SESSIONS_ROOT", "sessions")).resolve()
PI_HOST = os.environ.get("BENCHCAM_PI_HOST", "harrison@tatooine.local")
PI_ROOT = os.environ.get("BENCHCAM_PI_ROOT", "/home/harrison/benchcam/sessions")

HOST = "127.0.0.1"  # localhost ONLY — this dashboard has no auth
PORT = 8770

#: Session folder ids look like ``2026-06-23_20-17-17`` — start with a date.
#: Used to drop ``.active`` and other bookkeeping entries from ``ls``.
_SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_")

#: Common Windows install paths for VLC when it isn't on PATH.
_VLC_FALLBACKS = (
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
)


class ReviewerError(RuntimeError):
    """Raised for an expected, user-facing failure (shown in the status line)."""


# --------------------------------------------------------------------------- #
# Shell-outs to the existing CLI + the OS
# --------------------------------------------------------------------------- #

def _find_vlc() -> str | None:
    vlc = shutil.which("vlc")
    if vlc:
        return vlc
    for candidate in _VLC_FALLBACKS:
        if os.path.exists(candidate):
            return candidate
    return None


def open_in_player(path: Path) -> None:
    """Play a video file: VLC if available, else the OS default player.

    Always non-blocking (Popen / startfile) so a click never freezes the server.
    """
    vlc = _find_vlc()
    if vlc:
        subprocess.Popen([vlc, str(path)])
    else:
        os.startfile(str(path))  # type: ignore[attr-defined]  # Windows only


def list_pi_sessions() -> list[str]:
    """Return the Pi's session ids (newest first) via ``ssh PI_HOST ls -1``.

    Raises ReviewerError if the Pi can't be reached so the UI can show why.
    """
    try:
        proc = subprocess.run(
            ["ssh", PI_HOST, f"ls -1 {PI_ROOT}"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:  # ssh not installed, etc.
        raise ReviewerError(f"could not reach the Pi ({exc})") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "ssh failed").strip()
        raise ReviewerError(f"could not reach the Pi ({detail})")

    ids = [
        line.strip()
        for line in proc.stdout.splitlines()
        if _SESSION_RE.match(line.strip())
    ]
    # The id encodes the timestamp, so a plain reverse sort is newest-first.
    ids.sort(reverse=True)
    return ids


def ensure_local(session_id: str) -> Path:
    """Make sure the session is copied onto the laptop, then return its folder.

    Skips the copy if the folder already exists — a session is ~390MB, so we
    don't re-scp it on every click. Shells out to ``benchcam fetch`` (which owns
    the scp logic); raises ReviewerError on failure.
    """
    dest = LOCAL_ROOT / session_id
    if dest.is_dir():
        return dest
    # Pass the configured Pi host/root through so the fetch targets the same box
    # this dashboard listed from (its CLI defaults match, but env overrides win).
    cmd = [
        sys.executable, "-m", "benchcam", "fetch", session_id,
        "--no-open",
        "--sessions-root", str(LOCAL_ROOT),
        "--host", PI_HOST,
        "--remote-root", PI_ROOT,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dest.is_dir():
        raise ReviewerError("fetch failed — Pi reachable? id correct?")
    return dest


def render_review(session_id: str) -> Path:
    """Render review.mp4 for a local session via ``benchcam edit`` (synchronous).

    The transcode can take a while on a real clip; we block until it finishes so
    the button's "working…" state is honest. Raises ReviewerError on failure.
    """
    review = LOCAL_ROOT / session_id / "review.mp4"
    if review.exists():
        return review
    cmd = [
        sys.executable, "-m", "benchcam", "edit",
        "--session", session_id,
        "--sessions-root", str(LOCAL_ROOT),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not review.exists():
        detail = (proc.stderr or proc.stdout or "render failed").strip()
        raise ReviewerError(detail.splitlines()[-1] if detail else "render failed")
    return review


# --------------------------------------------------------------------------- #
# Endpoint actions (each returns a JSON-able dict; never raises to the handler)
# --------------------------------------------------------------------------- #

def api_sessions() -> dict:
    try:
        ids = list_pi_sessions()
    except ReviewerError as exc:
        return {"ok": False, "message": str(exc)}
    sessions = [
        {
            "id": sid,
            "local": (LOCAL_ROOT / sid).is_dir(),
            "has_review": (LOCAL_ROOT / sid / "review.mp4").exists(),
        }
        for sid in ids
    ]
    return {"ok": True, "sessions": sessions}


def api_folder(session_id: str) -> dict:
    dest = ensure_local(session_id)
    os.startfile(str(dest))  # type: ignore[attr-defined]  # opens Explorer (Windows)
    return {"ok": True, "message": f"opened folder for {session_id}"}


def api_watch(session_id: str) -> dict:
    dest = ensure_local(session_id)
    capture = dest / "capture.mkv"
    if not capture.exists():
        return {"ok": False, "message": "no capture.mkv in this session"}
    open_in_player(capture)
    return {"ok": True, "message": f"playing {session_id}"}


def api_review(session_id: str) -> dict:
    ensure_local(session_id)
    review = render_review(session_id)  # no-op if review.mp4 already exists
    open_in_player(review)
    return {"ok": True, "message": f"review ready for {session_id}"}


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

class ReviewerHandler(BaseHTTPRequestHandler):
    server_version = "BenchCamReviewer/1.0"

    def log_message(self, *args) -> None:  # keep the console quiet
        return

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

    def _read_id(self) -> str:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return str(json.loads(raw or b"{}").get("id", "")).strip()
        except ValueError:
            return ""

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_html(PAGE_HTML)
        elif path == "/api/sessions":
            self._send_json(api_sessions())
        else:
            self._send_json({"ok": False, "message": "not found"}, 404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        routes = {
            "/api/folder": api_folder,
            "/api/watch": api_watch,
            "/api/review": api_review,
        }
        action = routes.get(path)
        if action is None:
            self._send_json({"ok": False, "message": "not found"}, 404)
            return
        session_id = self._read_id()
        if not _SESSION_RE.match(session_id):
            self._send_json({"ok": False, "message": "bad or missing session id"})
            return
        try:
            self._send_json(action(session_id))
        except ReviewerError as exc:
            self._send_json({"ok": False, "message": str(exc)})
        except Exception as exc:  # never crash the server on one bad click
            self._send_json({"ok": False, "message": f"unexpected error: {exc}"})


# --------------------------------------------------------------------------- #
# Single-page UI (no external assets)
# --------------------------------------------------------------------------- #

PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BenchCam Reviewer (laptop)</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0f1115;
    --surface: #171a21;
    --surface-hover: #1c2029;
    --border: #262b36;
    --border-soft: #21262f;
    --text: #e6e8eb;
    --muted: #9aa3af;
    --accent: #3b82f6;
    --accent-hover: #2f6fe0;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
            "Liberation Mono", monospace;
  }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica,
         Arial, sans-serif; margin: 0; padding: 0 20px 64px; background: var(--bg);
         color: var(--text); line-height: 1.45;
         -webkit-font-smoothing: antialiased; }
  .wrap { max-width: 880px; margin: 0 auto; padding-top: 32px; }

  /* Header */
  h1 { font-size: 24px; font-weight: 700; letter-spacing: -0.01em;
       margin: 0 0 6px; }
  h1 .sub { font-size: 15px; font-weight: 500; color: var(--muted);
            margin-left: 6px; }
  .sub { color: var(--muted); font-size: 14px; margin: 0 0 4px; }
  .wrap > .sub { margin-bottom: 22px; max-width: 60ch; }

  /* Status banner */
  #status { min-height: 20px; padding: 12px 16px; border-radius: 12px;
            margin: 0 0 18px; background: var(--surface);
            border: 1px solid var(--border-soft); font-size: 14px;
            color: var(--muted); transition: background .15s, border-color .15s; }
  #status.ok { color: #6ee7b7; background: rgba(16, 185, 129, .08);
               border-color: rgba(16, 185, 129, .28); }
  #status.err { color: #fca5a5; background: rgba(239, 68, 68, .08);
                border-color: rgba(239, 68, 68, .30); }

  /* Toolbar (Refresh) */
  .toolbar { display: flex; justify-content: flex-end; margin: 0 0 14px; }

  /* Card-style table rows */
  table { width: 100%; border-collapse: separate; border-spacing: 0 10px;
          font-size: 14px; }
  thead th { text-align: left; padding: 0 16px 4px; color: var(--muted);
             font-weight: 600; font-size: 12px; text-transform: uppercase;
             letter-spacing: .06em; }
  tbody tr { background: var(--surface); transition: background .12s; }
  tbody tr:hover { background: var(--surface-hover); }
  tbody td { padding: 14px 16px; border-top: 1px solid var(--border);
             border-bottom: 1px solid var(--border); vertical-align: middle; }
  tbody td:first-child { border-left: 1px solid var(--border);
             border-top-left-radius: 12px; border-bottom-left-radius: 12px; }
  tbody td:last-child { border-right: 1px solid var(--border);
             border-top-right-radius: 12px; border-bottom-right-radius: 12px;
             white-space: nowrap; }
  code { font-family: var(--mono); font-size: 13px; background: #0c0e12;
         color: #cdd3db; padding: 3px 8px; border-radius: 6px;
         border: 1px solid var(--border-soft); }

  /* Status pills */
  .tag { display: inline-block; font-size: 12px; font-weight: 600;
         padding: 3px 10px; border-radius: 999px; margin-right: 6px;
         border: 1px solid transparent; }
  .tag.local { background: rgba(16, 185, 129, .12); color: #6ee7b7;
               border-color: rgba(16, 185, 129, .25); }
  .tag.pi { background: #1b1f27; color: var(--muted); border-color: var(--border); }
  .tag.rev { background: rgba(59, 130, 246, .12); color: #93c5fd;
             border-color: rgba(59, 130, 246, .28); }

  /* Buttons */
  button { cursor: pointer; border: 1px solid transparent; border-radius: 9px;
           padding: 9px 14px; font-size: 13px; font-weight: 600; color: #fff;
           background: var(--accent); margin: 2px; line-height: 1;
           transition: background .12s, border-color .12s, opacity .12s; }
  button:hover { background: var(--accent-hover); }
  button.ghost { background: transparent; color: var(--text);
                 border-color: var(--border); }
  button.ghost:hover { background: var(--surface-hover);
                       border-color: #323a47; }
  button:disabled, button:disabled:hover { opacity: .4; cursor: not-allowed;
           background: var(--surface); color: var(--muted);
           border-color: var(--border); }
  .refresh { background: transparent; color: var(--muted);
             border-color: var(--border); padding: 7px 14px; }
  .refresh:hover { background: var(--surface-hover); color: var(--text);
                   border-color: #323a47; }

  @media (max-width: 560px) {
    tbody td { padding: 12px; }
    tbody td:last-child { white-space: normal; }
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>BenchCam Reviewer <span class="sub">(laptop)</span></h1>
  <div class="sub">Sessions live on the Pi. These buttons copy one here on demand,
     then open / play / render it on this machine.</div>
  <div id="status">Loading sessions from the Pi&hellip;</div>
  <div class="toolbar"><button class="refresh" onclick="loadSessions()">Refresh</button></div>
  <table>
    <thead><tr><th>session</th><th>status</th><th>actions</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <p id="empty" class="sub" style="display:none">No sessions found on the Pi.</p>
</div>

<script>
const $ = (id) => document.getElementById(id);

function setStatus(msg, ok) {
  const s = $("status");
  s.textContent = msg;
  s.className = ok === undefined ? "" : (ok ? "ok" : "err");
}

async function loadSessions() {
  try {
    const res = await fetch("/api/sessions");
    const data = await res.json();
    if (!data.ok) { setStatus(data.message || "could not list sessions", false); renderRows([]); return; }
    setStatus(data.sessions.length + " session(s) on the Pi.", true);
    renderRows(data.sessions);
  } catch (e) {
    setStatus("could not reach the reviewer server: " + e, false);
  }
}

function renderRows(sessions) {
  const tb = $("rows");
  tb.innerHTML = "";
  $("empty").style.display = sessions.length ? "none" : "block";
  sessions.forEach(s => tb.appendChild(row(s)));
}

function tag(text, cls) {
  const span = document.createElement("span");
  span.className = "tag " + cls; span.textContent = text; return span;
}

function row(s) {
  const tr = document.createElement("tr");

  const tdId = document.createElement("td");
  const code = document.createElement("code"); code.textContent = s.id;
  tdId.appendChild(code);

  const tdStatus = document.createElement("td");
  tdStatus.appendChild(s.local ? tag("local", "local") : tag("on Pi", "pi"));
  if (s.has_review) tdStatus.appendChild(tag("review ✓", "rev"));

  const tdAct = document.createElement("td");
  tdAct.appendChild(actionButton("Open folder", "ghost", "/api/folder", s.id));
  tdAct.appendChild(actionButton("Watch", "", "/api/watch", s.id));
  tdAct.appendChild(actionButton("Make review", "ghost", "/api/review", s.id));

  [tdId, tdStatus, tdAct].forEach(td => tr.appendChild(td));
  return tr;
}

function actionButton(text, cls, endpoint, id) {
  const b = document.createElement("button");
  b.textContent = text; b.className = cls;
  b.onclick = async (ev) => {
    // Capture the EXACT clicked button and its label BEFORE the await. The
    // finally restores this element by direct reference — never by CSS class,
    // since a styling pass can rename classes and a class-based re-select would
    // then miss the button and leave it stuck on "working…" forever.
    // (currentTarget is only valid during dispatch, so it must be read now.)
    const btn = ev.currentTarget;
    const original = btn.textContent;
    btn.disabled = true; btn.textContent = "working…";
    let ok = false;
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({id: id}),
      });
      const data = await res.json();
      setStatus(data.message || (data.ok ? "done" : "failed"), !!data.ok);
      ok = true;
    } catch (e) {
      setStatus("request failed: " + e, false);
    } finally {
      // ALWAYS re-enable the exact button we disabled and restore its label,
      // regardless of CSS class or any pending table rebuild.
      btn.disabled = false; btn.textContent = original;
    }
    // Refresh the list AFTER the button is restored, so rebuilding the table can
    // never strand a button mid-flight in the disabled "working…" state.
    if (ok) loadSessions();
  };
  return b;
}

loadSessions();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> int:
    LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
    print("BenchCam Reviewer (laptop-side)")
    print(f"  local sessions root : {LOCAL_ROOT}")
    print(f"  Pi host             : {PI_HOST}")
    print(f"  Pi sessions root    : {PI_ROOT}")
    print(f"  serving on          : http://{HOST}:{PORT}/  (localhost only, no auth)")
    print("Open that URL in your browser. Press Ctrl+C to stop.")
    httpd = HTTPServer((HOST, PORT), ReviewerHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping reviewer.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
