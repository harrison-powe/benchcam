# BenchCam

catching actuators in the act

BenchCam is a **local-first**, bench-side capture and marker logging tool for
hardware work. You start a session, log time-stamped markers as you work, and
everything is saved as plain local files you can read with any text editor.

## What BenchCam is (and is not)

- It records a session and logs markers (events with a timestamp + label).
- It keeps everything in local files only. **No cloud sync.**
- It can drive a recorder to capture video (an ffmpeg subprocess on Windows, or
  OBS Studio over OBS WebSocket).
- It **does not** control any moving hardware or actuators. BenchCam may later
  receive external marker events, but it stays strictly on the observation side.

v0 ships with a **NullRecorder** (records no video) so you can use and test the
session + marker workflow immediately, with or without a camera. The
**FfmpegRecorder** records real video from a webcam on Windows (see
[Recording video with ffmpeg](#recording-video-with-ffmpeg)), and the
**ObsRecorder** drives OBS Studio's recording (see
[Recording video with OBS](#recording-video-with-obs)).

## Requirements

- Python 3.11 or newer.
- For video recording: a working `ffmpeg` binary on your `PATH` (only needed if
  you use `--recorder ffmpeg`; the default `null` recorder needs nothing extra).
- For the OBS recorder: OBS Studio 28+ and the optional `benchcam[obs]` extra
  (only needed if you use `--recorder obs`). See
  [Recording video with OBS](#recording-video-with-obs).

## Install (Windows v0)

Open **PowerShell** (or **Command Prompt**) in the project folder.

```powershell
# 1. Create and activate a virtual environment
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install BenchCam (editable)
pip install -e .
```

If PowerShell blocks the activation script, allow it for the current user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

You can also run BenchCam without installing the console script:

```powershell
python -m benchcam --help
```

## Usage (Windows v0)

A session is just four commands. Run them from the same folder so they share the
same `sessions\` directory.

```powershell
# Create a new session (uses the NullRecorder by default)
benchcam new --profile bench-a

# Start the session clock
benchcam run

# Log markers as you work (quote labels that contain spaces)
benchcam mark "power on"
benchcam mark "chip lifted"
benchcam mark "fault observed"

# Close the session
benchcam end
```

### Where sessions are stored

By default session folders are created under `.\sessions\` in the current
directory. To put **all** session data (markers and collected videos) somewhere
else — for example an external SSD — set the `BENCHCAM_SESSIONS_ROOT`
environment variable. Every command honors it, so new sessions and the videos
collected into them land there with no code change and no per-command flag:

```powershell
# Persist it for your user (new shells pick it up automatically):
setx BENCHCAM_SESSIONS_ROOT "E:\benchcam-sessions"

# ...or just for the current shell:
$env:BENCHCAM_SESSIONS_ROOT = "E:\benchcam-sessions"
```

An explicit `--sessions-root PATH` on any command overrides the env var, which
overrides the `.\sessions\` default. No drive letter is hardcoded — point it at
whatever drive you like (the external SSD is the intended home once it's
available).

### Fast marking with `benchcam live`

`new`/`run`/`mark`/`end` each run as a separate process, which is fine for
occasional marks but slow when your hands are busy at the bench. `benchcam live`
opens a single long-running shell that holds the active session in memory and
marks on **one keypress** — no per-mark process startup, no re-reading
`markers.csv`.

```powershell
# Create a session first (if you don't already have an active one)
benchcam new --profile bench-a

# Enter the live shell (starts the session if it hasn't started yet)
benchcam live
```

Inside the shell:

| Key | Action |
| --- | --- |
| `Space` / `Enter` | Mark **now**, no label. Fast path — prints index + elapsed seconds instantly. |
| `l` | Mark now, then type a one-line label (empty label is allowed). |
| `n` | Append a line to `notes.md` (fills the gap where notes were write-once). |
| `s` | Show status: session id, elapsed time, marker count so far. |
| `q` | Quit cleanly: stop the recorder, end the session, print a summary. |

Notes:

- `live` attaches to the same active session as the other commands. If the
  session is still `created`, it starts it (same effect as `run`, including
  `recorder.start`). If it has already `ended`, it refuses with a clear message.
- Every mark is still **appended to `markers.csv` immediately** for crash
  safety; only the next marker index is tracked in memory.
- Markers made here use `source = manual`, exactly like `benchcam mark`.

This produces a folder like:

```
sessions\2026-06-17_05-43-00\
    session.json
    markers.csv
    notes.md
```

### session.json

```json
{
  "session_id": "2026-06-17_05-43-00",
  "created_wall_time": "2026-06-17T05:43:00.123456+00:00",
  "profile": "bench-a",
  "camera": "",
  "microphone": "",
  "recorder": "null",
  "storage_path": "sessions\\2026-06-17_05-43-00",
  "notes": "",
  "status": "ended",
  "started_wall_time": "2026-06-17T05:43:10.000000+00:00",
  "ended_wall_time": "2026-06-17T05:45:00.000000+00:00"
}
```

### markers.csv

```csv
marker_index,elapsed_seconds,wall_time,source,label
1,2.500,2026-06-17T05:43:12.500000+00:00,manual,power on
2,15.250,2026-06-17T05:43:25.250000+00:00,manual,chip lifted
```

- `elapsed_seconds` is measured from when you ran `benchcam run`.
- `source` is `manual` for `benchcam mark`; a future external feed can use other
  sources.

### notes.md

A free-form Markdown file for whatever you want to jot down during the session.

## CLI reference

| Command | What it does |
| --- | --- |
| `benchcam new` | Create a new session folder and make it the active session. |
| `benchcam run` | Start recording / start the session clock. |
| `benchcam mark "label"` | Append a time-stamped marker to the active session. |
| `benchcam live` | Open an interactive shell that marks the active session on a single keypress. |
| `benchcam end` | Stop recording and close the active session. |
| `benchcam edit` | Render a marker-aware `review.mp4` (timelapse + normal-speed marker windows + captions). |
| `benchcam dashboard` | Start a local web UI to run a whole session (start/mark/stop/review) from a browser. |

Useful options:

- `benchcam new --profile NAME --camera DESC --microphone DESC --recorder {null,obs,ffmpeg} --notes "..."`
- `benchcam mark "label" --source external`
- `benchcam edit --session ID --pre 3 --post 5 --speed 8 --font PATH`
- `--sessions-root PATH` (on any command) to use a different sessions directory.

The "active" session is tracked by a small pointer file at
`sessions\.active`, so `run`, `mark`, and `end` know which session to use.

## Recorders

- **NullRecorder** (`null`, default): records no video. Lets you exercise the
  session and marker workflow, and pairs well with capturing video manually in a
  separate app.
- **ObsRecorder** (`obs`): drives OBS Studio's recording over OBS WebSocket v5
  (optional `benchcam[obs]` extra). See
  [Recording video with OBS](#recording-video-with-obs).
- **FfmpegRecorder** (`ffmpeg`): records one video file per session by driving an
  external `ffmpeg` binary. Windows (DirectShow / dshow) is the supported target;
  see [Recording video with ffmpeg](#recording-video-with-ffmpeg).

See `src/benchcam/recorders/` for the recorder code.

## Recording video with ffmpeg

The `ffmpeg` recorder captures one video file, **`capture.mp4`**, into the
session folder. It starts when the session starts, so the video timecode lines
up with each marker's `elapsed_seconds`. ffmpeg is run as an external binary —
BenchCam adds no Python ffmpeg dependency, so you must have `ffmpeg` on your
`PATH` (`winget install Gyan.FFmpeg`, or download from
[ffmpeg.org](https://ffmpeg.org/download.html) and add it to `PATH`).

### 1. Find your camera's device name

The DirectShow device name (for a Logitech C920S it is usually
`HD Pro Webcam C920S`) must be passed exactly. List the devices on your machine:

```powershell
ffmpeg -list_devices true -f dshow -i dummy
```

Look under "DirectShow video devices" for the quoted name of your webcam.

### 2. Start a session that records from the camera

Pass the device name with `--camera`; it is stored in `session.json` and used as
the source of truth for the device. (You can also set the `BENCHCAM_CAMERA`
environment variable; an explicit `--camera` on the session wins.)

```powershell
benchcam new --recorder ffmpeg --camera "HD Pro Webcam C920S" --profile bench-a
benchcam live
```

`benchcam live` (or `benchcam run`) launches ffmpeg in the background and returns
immediately; quitting `live` (or `benchcam end`) sends `q` to ffmpeg so the MP4
is finalized and playable, force-killing only if it does not exit in time. If
`ffmpeg` is missing from `PATH`, or no camera name is configured, the command
fails with a clear message instead of silently recording nothing.

Defaults are tuned for the C920S: 1080p30 over MJPEG, H.264 (`libx264`), video
only (no audio). An `ffmpeg.log` is written next to the video for troubleshooting.

> POSIX note: Linux (v4l2) and macOS (avfoundation) input paths are marked TODO
> in `build_ffmpeg_command`; Windows is the supported target for v0.

## Recording video with OBS

The `obs` recorder lets BenchCam drive **OBS Studio**'s recording over OBS
WebSocket v5. OBS stays your live dashboard (preview, framing, focus) while
BenchCam tells it when to start and stop. Because BenchCam triggers the start,
marker `elapsed_seconds` lines up with the OBS video timecode automatically.

**OBS writes the video to its own configured recording folder** (BenchCam can't
change where OBS records). So when the session ends, BenchCam **collects** that
video — it *moves* the file into the session folder as `capture.<ext>` (the
extension is whatever OBS wrote, `.mkv` or `.mp4`), so the video ends up right
next to `markers.csv` / `session.json` / `notes.md` and each session folder is
self-contained. The `obs_recording.txt` pointer and a `notes.md` line are updated
to the new in-folder path.

If the move can't happen (file still locked, drive missing, etc.), the session
still ends cleanly and the pointer keeps the original OBS path — collection
degrades to "pointer to the external file", never to lost footage. The move is
cross-drive safe (it falls back to copy-then-delete), so it works even when OBS
records on `C:` and your session root is on an external SSD.

> Single-app camera constraint: with the OBS recorder, **OBS owns the camera**
> and provides the live preview. Do not also run the ffmpeg recorder against the
> same C920S — only one app can hold the camera at a time.

### 1. Install OBS and enable the WebSocket server

1. Install OBS Studio 28 or newer (WebSocket v5 is built in — no plugin needed).
2. In OBS: **Tools → WebSocket Server Settings**.
3. Check **Enable WebSocket server**. Note the **Server Port** (default `4455`).
4. Click **Show Connect Info** to see / copy the **Server Password** (auth is on
   by default).

### 2. Install the optional extra and set the password

The OBS client (`obsws-python`) is an **optional** dependency — the core BenchCam
install needs nothing third-party. Install the extra and pass the password via an
environment variable (never commit it):

```powershell
pip install -e ".[obs]"

# Connection config (constructor arg > env vars > defaults). Set at least the
# password; host/port default to localhost/4455.
$env:BENCHCAM_OBS_PASSWORD = "<the password from Show Connect Info>"
# Optional overrides:
# $env:BENCHCAM_OBS_HOST = "localhost"
# $env:BENCHCAM_OBS_PORT = "4455"
```

If `obsws-python` isn't installed, or OBS isn't running / reachable, the `obs`
recorder fails with a clear, actionable error instead of silently recording
nothing.

### 3. Run a session with the OBS recorder

```powershell
benchcam new --recorder obs --profile bench-a
benchcam live
```

Entering `live` (or `benchcam run`) connects to OBS and sends `StartRecord`;
quitting `live` (or `benchcam end`) sends `StopRecord`, captures the file path OBS
wrote, and disconnects. If OBS is *already* recording when you start, BenchCam
refuses (so you don't end up with a second, misaligned recording).

After the session, the OBS video is `sessions\<id>\capture.mkv` (or `.mp4`) next
to `markers.csv`, `obs_recording.txt` records its final path, and the marker
`elapsed_seconds` values map directly onto that recording's timecode.

## Auto-editing a review clip (`benchcam edit`)

`benchcam edit` turns a recorded session into a YouTube-ready "build log"
`review.mp4` with no manual editing:

- The stretches between markers are **timelapsed** (default `--speed 8` for 8x).
- Around each marker the clip drops to **normal speed** for a window — default
  `--pre 3` seconds before and `--post 5` seconds after. Overlapping or adjacent
  windows merge into one normal-speed segment.
- **Audio** is kept in the normal-speed windows (your narration) and dropped in
  the timelapsed stretches (no chipmunk audio).
- Each marker that has a **label** gets that label burned on screen as a caption
  during its normal-speed window.

It reads the session's `capture.*` and `markers.csv` and writes `review.mp4` into
the **same** session folder. It needs `ffmpeg` (and `ffprobe`, which ships with
ffmpeg) on your `PATH` — see [Recording video with ffmpeg](#recording-video-with-ffmpeg)
for install instructions.

```powershell
# Edit the newest session with the defaults (3s pre / 5s post / 8x):
benchcam edit

# ...or a specific session, with custom pacing:
benchcam edit --session 2026-06-18_05-43-00 --pre 2 --post 6 --speed 12

# Custom caption font (optional):
benchcam edit --font "C:\Windows\Fonts\arial.ttf"
```

Captions are rendered by ffmpeg's `drawtext`. BenchCam escapes the caption text
and the font path for ffmpeg's filtergraph automatically (so Windows paths like
`C:\Windows\Fonts\arial.ttf` and labels containing `:`, `'`, `,`, `\`, or `%`
render correctly), and passes the filtergraph via a temp filterscript. If the
chosen `--font` isn't found, it falls back to a common system font (Arial on
Windows), and finally to ffmpeg's default — the render never dies on a missing
font. By default captions use `arial.ttf` on Windows; pass `--font` to override.

`benchcam edit` first prints the **segment plan** (which stretches are timelapsed
vs. normal speed, and which captions land where) so you can sanity-check the
pacing before watching, then renders `sessions\<id>\review.mp4`. Re-running
overwrites `review.mp4` cleanly; `capture.*` is never modified or deleted.

Notes:

- **No markers** → a straight `--speed` timelapse of the whole video (still a
  quick way to skim a session); `edit` says so.
- Marker times past the end of the video are clamped to its length.
- If `capture.*` is missing (e.g. an OBS session where collection failed and only
  `obs_recording.txt` remains), `edit` follows that pointer to the original file;
  if it still can't be found, it fails with a clear message.

## Web dashboard (run a session without a terminal)

`benchcam dashboard` starts a small **local** web server (stdlib `http.server`,
bound to `127.0.0.1:8765`, no accounts, no external exposure) and opens your
browser to a one-page UI. From there you can start a session, mark events (click
or labeled), add notes, stop, and render the review clip — without typing any
terminal commands.

```powershell
benchcam dashboard
```

The page has a clear **● RECORDING / ○ IDLE** indicator so you always know the
state, a big **MARK** button (plus a label field), a notes field, **Stop**, and a
**Make review.mp4** button (with `pre`/`post`/`speed` fields). It calls the same
BenchCam logic as the CLI — start = `new` + `run`, mark = `mark`, stop = recorder
stop + collect + `end`, review = `edit`. If OBS isn't running, starting an OBS
session shows a clear error rather than silently failing; a second start is
refused; stopping when nothing is active is a no-op with a message.

> The dashboard's MARK button is a click convenience. For hands-busy marking,
> `benchcam live` in a terminal (single keypress per marker) is still the fastest
> path — the dashboard does not replace it.

With the OBS recorder, **OBS itself is your live camera preview** next to the
dashboard; BenchCam just drives OBS's record start/stop so markers stay aligned.

### Launcher with no console window (recommended)

For a clean double-click that just opens the dashboard — **no console window,
no flash** — use `scripts\benchcam-dashboard.vbs`. It runs `benchcam dashboard`
with the venv's `pythonw.exe` (the windowless Python), so nothing visible appears
while the server runs in the background for your session.

**Desktop shortcut (a):**

1. In File Explorer, open the project's `scripts\` folder.
2. Right-click **`benchcam-dashboard.vbs`** → **Show more options** → **Send to**
   → **Desktop (create shortcut)**. Double-clicking it now opens the dashboard
   with no console window.

**Pin to the taskbar (b):** A shortcut to a `.vbs` can't be pinned directly, but
a shortcut to **`wscript.exe`** can (it's an `.exe`). So point the shortcut at
`wscript.exe` and pass the script as an argument:

1. Right-click the desktop → **New → Shortcut**.
2. For the location, enter (use YOUR full path to the repo):

   ```text
   wscript.exe "C:\path\to\benchcam\scripts\benchcam-dashboard.vbs"
   ```

3. Name it **BenchCam**, finish, then right-click it → **Pin to taskbar**
   (or **Pin to Start**). Because the target is `wscript.exe`, pinning is allowed.

**Custom icon (c):** Right-click the shortcut → **Properties** → **Change
Icon…** → browse to your `.ico` → **OK**. The icon shows on the desktop shortcut
and on the pinned taskbar button.

**Stopping it:** because there's no window, stop the dashboard from **Task
Manager** → end the background **`pythonw.exe`** process running BenchCam (or just
leave it; it's a tiny local server). If a double-click seems to do nothing, the
dashboard is probably already running — open <http://127.0.0.1:8765> in your
browser, or stop the existing `pythonw.exe` first.

> The launcher assumes a `.venv` in the project root with BenchCam installed
> (`py -3 -m venv .venv` → activate → `pip install -e .`, plus
> `pip install -e ".[obs]"` if you use the OBS recorder).

### Alternative launcher: the `.bat` (shows a console)

`scripts\benchcam-dashboard.bat` (and the PowerShell `scripts\benchcam-dashboard.ps1`)
activate the venv and run `benchcam dashboard` too, but they keep a **console
window** open (closing it stops the dashboard, which is handy). To use it: open
`scripts\`, right-click **`benchcam-dashboard.bat`** → **Send to → Desktop
(create shortcut)**, and optionally set the shortcut's **Run:** to **Minimized**.
Prefer the `.vbs` launcher above for a no-console, pinnable experience.

### Run a full session from the dashboard

1. (OBS recorder) Open OBS with your C920S as a source so you have a live
   preview, and make sure its WebSocket server is enabled and
   `BENCHCAM_OBS_PASSWORD` is set (see
   [Recording video with OBS](#recording-video-with-obs)).
2. Double-click the desktop shortcut → the dashboard opens in your browser.
3. Pick the recorder (OBS by default), optionally type a profile, click
   **Start session**. The indicator turns red **● RECORDING**.
4. Work at the bench: click **MARK now** for quick markers, or type a label and
   click **Mark + label**; add free-form notes with the note field. Markers
   appear in the live list with their elapsed time.
5. Click **Stop session** → the indicator returns to **○ IDLE** and a summary
   (marker count, duration, folder) appears.
6. Click **Make review.mp4** → BenchCam renders the marker-aware review clip into
   the session folder and shows its path.

### 30-second manual test (Windows 11)

From the project folder, in PowerShell, with BenchCam installed:

```powershell
benchcam new --profile quicktest
benchcam live
```

Then, inside the live shell:

1. Press `Space` twice — you should see `marker #1` and `marker #2` with elapsed
   seconds.
2. Press `l`, type `chip lifted`, press `Enter` — you should see `marker #3 ... chip lifted`.
3. Press `n`, type `looks good`, press `Enter` — confirms a note was appended.
4. Press `s` — prints the session id, elapsed time, and `markers 3`.
5. Press `q` — prints a summary and exits.

Finally, open the newest folder under `sessions\` and confirm `markers.csv` has
three rows (two blank labels, one `chip lifted`) and `notes.md` ends with
`looks good`.

## Development

```powershell
pip install -e ".[dev]"
pytest
```

## Data and privacy

- BenchCam writes only to your local `sessions\` directory.
- Video/media files (each session's `capture.mp4`/`capture.mkv`, whether written
  by ffmpeg or collected from OBS) and the `sessions\` directory are
  **git-ignored** and must not be committed. Video is large — keep recordings on
  your external SSD (set `BENCHCAM_SESSIONS_ROOT`), not in git.

## Project layout

```
src/benchcam/
    cli.py            argparse CLI (new/run/mark/live/end/edit/dashboard)
    session.py        session model + on-disk layout
    markers.py        markers.csv reading/writing
    live.py           interactive single-keypress marking shell
    keypress.py       cross-platform single-key reader (msvcrt / termios)
    editor.py         marker-aware auto-edit -> review.mp4 (ffmpeg)
    dashboard.py      local web UI (stdlib http.server) over the existing logic
    clock.py          time helpers
    recorders/
        base.py       Recorder interface
        null.py       NullRecorder (default)
        obs.py        ObsRecorder (OBS Studio via OBS WebSocket v5)
        ffmpeg.py     FfmpegRecorder (ffmpeg subprocess; Windows/dshow)
        collect.py    move an external recording into the session folder
scripts/
    benchcam-dashboard.vbs   no-console Windows launcher (recommended; pinnable)
    benchcam-dashboard.bat   Windows launcher (keeps a console window)
    benchcam-dashboard.ps1   PowerShell launcher
tests/                unit tests
```

## License

MIT.
