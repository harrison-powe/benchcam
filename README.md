# BenchCam

catching actuators in the act

BenchCam is a **local-first**, bench-side **capture + event-marking workflow**
for documenting hardware work. You start a session, log time-stamped markers as
you work (e.g. "power on", "chip lifted", "fault"), optionally record video with
an **off-the-shelf webcam**, and end up with a self-contained folder of plain
files you can read with any text editor — plus an optional auto-edited review
clip that timelapses the boring parts and slows down around each marker.

> **Status:** v0, now exercised on **real bench sessions**. Two real
> `hello-motion` sessions exist — a connection-debugging session and one where the
> actuator was recorded **spinning on command** (open-loop bring-up, narrated on
> camera). The capture path, the physical **START/MARK/STOP/MUTE** buttons with
> their record/mute LEDs and OLED status panel, and the full post-capture pipeline
> (transcribe → auto-chapter → speech-aware review clip) have all run on real
> footage. Honest caveats: the physical controls still run on the **breadboard**
> rig — a v1 control PCB is built and electrically verified but has **never
> successfully driven anything**
> (see [The v1 control PCB](#the-v1-control-pcb-built-and-verified-not-yet-working)) —
> the actuator sessions were bring-up/debugging rather than a polished showcase,
> and raw capture still records to a **microSD** until the external SSD is
> deployed. Treat it as an early, working tool, not a finished product.

## What BenchCam is (and is not)

- It is a **software workflow**: it records a session, logs markers (events with
  a timestamp + label), and can auto-edit a review clip. Everything is kept in
  local files only — **no cloud sync, no accounts**.
- For video it drives **existing, off-the-shelf tools**: an `ffmpeg` subprocess
  capturing a USB webcam, or OBS Studio over OBS WebSocket. **BenchCam does not
  build, sell, or include any camera, capture card, or hardware** — you supply a
  regular webcam and (optionally) OBS or ffmpeg.
- It is **not** a camera system and **not** a robotics or motion-control system.
  It uses off-the-shelf cameras and compute (a USB webcam, a Raspberry Pi) and
  does **not** build a camera, capture card, image sensor, or high-speed video
  electronics. It **does** have physical controls — tactile START/MARK/STOP/MUTE
  buttons, record/mute status LEDs, and a small I²C status display wired to the
  Pi's GPIO — and it switches **one bench ring light** (an illumination load, through
  a low-side MOSFET) so the light follows the recording. That is the entire extent
  of what it drives: it stays on the **observation** side and **never commands
  actuators or any moving hardware** (it may *receive* external marker events, but
  never commands motion).

v0 ships with a **NullRecorder** (records no video) so you can use and test the
session + marker workflow immediately, with or without a camera. The
**FfmpegRecorder** records real, audio-synced video by driving an external
`ffmpeg`: on **Linux / Raspberry Pi** it captures the webcam over V4L2 and the mic
over ALSA into `capture.mkv` — this is the primary bench setup — and on
**Windows** it captures over DirectShow (see
[Recording video with ffmpeg](#recording-video-with-ffmpeg)). The **ObsRecorder**
drives OBS Studio's recording on Windows (see
[Recording video with OBS](#recording-video-with-obs)).

## How it's deployed (two machines)

In the real bench setup the work is split across two machines:

- **Capture appliance — Raspberry Pi 5 (Linux).** The camera, ring light, and mic
  live at the bench on the Pi. It records over V4L2/ALSA, exposes the physical
  GPIO controls, and runs the dashboard and the button daemon as always-on
  `systemd` services (both survive reboot). Marking a session never needs the
  laptop.
- **Compute / edit / upload — Windows laptop.** Whisper transcription, silero-VAD,
  the `edit` render, and the Claude `label`/`autochapter` API calls run here (on
  the GPU). `benchcam fetch <id>` pulls a session off the Pi over `scp`; only the
  small finished `review.mp4` is uploaded.

The heavy, torch-based extras (`[transcribe]`, `[vad]`) and the `[label]` extra are
**laptop-only** — the Pi install stays stdlib-only so capture has no heavy deps.

### Physical controls (GPIO)

Four tactile buttons, two status LEDs, an I²C status display, and one switched
bench light wire to the Pi's 40-pin header (3.3V logic only):

| Function | GPIO | What it does |
| --- | --- | --- |
| **START** | 27 | create a session and start recording |
| **MARK** | 17 | drop a frame-accurate `source=gpio` marker |
| **STOP** | 22 | stop recording and end the session |
| **MUTE** | 23 | toggle a mute span (force-timelapsed out of the review) |
| record LED | 5 | lit while a session is RUNNING |
| mute LED | 24 | lit while a mute span is open |
| ring light | 6 | bench light through an IRLZ44N low-side MOSFET; on while RUNNING |
| OLED | I²C-1 | SSD1306 128×64 at `0x3C`: elapsed, marker count, mute badge, free space |

The Pi-local daemon [gpio_buttons.py](gpio_buttons.py) debounces the presses and
calls the same session/marker code the CLI uses, so button-driven and CLI-driven
sessions are cross-process safe. It runs as the always-on `benchcam-buttons`
service. Everything below is validated on the **breadboard** rig:

- The **record LED** is derived from the on-disk session state rather than
  in-memory state, so it stays correct across a daemon restart.
- The **status display** shows elapsed time, marker count, a mute badge, and free
  space with a low-space flag — measured against the same pre-flight floor the
  recorder enforces, so the panel can never show a healthy number while START
  would refuse. It refreshes on a background thread, fails soft (no display fault
  can affect capture), and retries a lost panel every 30s.
- **SIGTERM/SIGINT** clears both LEDs and the panel, so a stopped daemon never
  leaves a lit record light or a frozen `●REC` frame on the bench.
- **Mute spans survive a daemon restart** via a session-local sidecar
  (`mute_open.json`) that holds only the currently-open span. `mute_spans.csv`
  itself stays append-only and complete-rows-only, so the renderer never sees a
  half-written span and mistakes it for one running to end-of-session.
- The **ring light** follows the recording through the MOSFET. The circuit is
  validated, but the light currently on the bench has a capacitive touch switch
  that resets on power loss — so the MOSFET can turn it off and never back on.
  Blocked on swapping in a light with a mechanical switch, not on hardware or code.

### The v1 control PCB (built and verified, not yet working)

A single-sided control board was designed and CNC-milled in-house from the KiCad
project in [control-board/](control-board/): 100 × 80 mm FR-1, four buttons,
record/mute LEDs, a 4-pin I²C header for the OLED, an IRLZ44N low-side MOSFET on
a screw terminal for the bench light, and a 2×20 IDC ribbon to the Pi header.
Designed 2026-07-18, milled 07-19, continuity-verified 07-26, fully soldered and
metered 08-02. Because it was milled rather than fabbed, the gerbers and mill
gcode are tracked alongside the design as the reproduction record.

**It has never successfully driven anything.** First bring-up crashed the Pi
twice, and a J1 ground path is intermittent; diagnosis is ongoing. The
**breadboard rig remains the live control surface** for every session.

As-built deltas, recorded so future debugging doesn't chase them:

- **R3 is 220Ω on the board, not the 150Ω in the schematic** (MOSFET gate series
  resistor). The schematic deliberately keeps the designed value.
- A deliberate **solder bridge spans R3 pad 2 to R4 pad 1** — both are already on
  the gate net; it repairs a trace broken during rework. It is a repair, not a
  design change, and a re-mill would not reproduce it.
- **Only the J1 pins that carry a net are soldered** (21 of 40); the other 19 are
  unconnected in the netlist.

## Requirements

- **Python 3.11 or newer** (developed and validated on Python 3.13). The core is
  pure standard library — no third-party packages are required to install or run
  the session/marker workflow or the dashboard.
- For video recording: a working `ffmpeg` binary on your `PATH` (only needed if
  you use `--recorder ffmpeg`; the default `null` recorder needs nothing extra).
- For the OBS recorder: OBS Studio 28+ and the optional `benchcam[obs]` extra
  (only needed if you use `--recorder obs`). See
  [Recording video with OBS](#recording-video-with-obs).
- A regular **USB webcam** if you want video (BenchCam does not provide one).

## Install (Windows / laptop)

The steps below set up the **laptop** (editing, transcription, upload). The
**Raspberry Pi capture box** uses the same editable install on Linux (`pip install
-e .`, no heavy extras) — see
[Recording headless on a Raspberry Pi](#3-recording-headless-on-a-raspberry-pi-linux--v4l2--alsa)
for the capture side.

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
marker_index,elapsed_seconds,wall_time,source,label,narration
1,2.500,2026-06-17T05:43:12.500000+00:00,gpio,Power Connected,so I've got power to the board now
2,15.250,2026-06-17T05:43:25.250000+00:00,manual,chip lifted,
```

- `elapsed_seconds` is measured from when the session started (`benchcam run`).
- `source` records where the marker came from: `manual` (`benchcam mark` or the
  dashboard), `gpio` (a physical button press), or `auto` (proposed by
  `benchcam autochapter`). `gpio`/`manual` are ground truth and win over `auto`
  on conflict.
- `label` is the short on-screen chapter title. `narration` is the raw
  transcribed speech near the marker (written by `benchcam transcribe`), kept in
  its own column so transcription and labeling never clobber each other.

### notes.md

A free-form Markdown file for whatever you want to jot down during the session.

## CLI reference

| Command | What it does |
| --- | --- |
| `benchcam new` | Create a new session folder and make it the active session. |
| `benchcam run` | Start recording / start the session clock. |
| `benchcam mark "label"` | Append a time-stamped marker to the active session. |
| `benchcam live` | Interactive shell that marks the active session on a single keypress. |
| `benchcam end` | Stop recording and close the active session. |
| `benchcam transcribe` | Fill each marker's `narration` from the audio with Whisper *(laptop; `[transcribe]` extra)*. |
| `benchcam label` | Summarize each marker's narration into a terse technical `label` via the Claude API *(laptop; `[label]` extra + `$ANTHROPIC_API_KEY`)*. |
| `benchcam autochapter` | Propose chapters from the **full-session** transcript and write them as `source=auto` markers; merges with real markers, which win *(laptop; `[transcribe]`+`[label]`)*. |
| `benchcam chapters` | Review/re-title chapters without re-watching: print, or `--edit`/`--apply` an editable `chapters.txt` (updates only the `label` column). |
| `benchcam edit` | Render a marker-aware `review.mp4` (timelapse + normal-speed windows + persistent chapter tags). Flags: `--vad`, `--auto`, `--preview`, `--fetch`. |
| `benchcam publish` | Export a paste-ready YouTube chapter block (remapped review timestamps) to `youtube_chapters.txt`. Read-only; never uploads. |
| `benchcam fetch` | Pull a recorded session off the Pi over `scp` to the laptop and open it *(run on the laptop)*. |
| `benchcam dashboard` | Local web UI to run a whole session (start/mark/stop/review) from a browser. |

Useful options:

- `benchcam new --profile NAME --camera DESC --microphone DESC --recorder {null,obs,ffmpeg} --notes "..."`
- `benchcam mark "label" --source external`
- `benchcam edit --session ID --pre 3 --post 5 --speed 30 [--vad] [--auto] [--preview] [--fetch] --font PATH`
- `--sessions-root PATH` (on any command) to use a different sessions directory.

Roughly, a full session flows: **capture** (Pi: `run`/`mark`/`end` or the GPIO
buttons) → **fetch** to the laptop → **transcribe** + **label** *or* **autochapter**
→ **edit** → optionally **chapters** to re-title → **publish** the chapter block.
`benchcam edit --auto` runs the transcribe → autochapter → render chain in one
command, skipping any step whose output already exists.

The "active" session is tracked by a small pointer file at
`sessions\.active`, so `run`, `mark`, and `end` know which session to use.

## Recorders

- **NullRecorder** (`null`, default): records no video. Lets you exercise the
  session and marker workflow, and pairs well with capturing video manually in a
  separate app.
- **ObsRecorder** (`obs`): drives OBS Studio's recording over OBS WebSocket v5
  (optional `benchcam[obs]` extra). See
  [Recording video with OBS](#recording-video-with-obs).
- **FfmpegRecorder** (`ffmpeg`): records one capture file per session by driving
  an external `ffmpeg` binary. Two paths are supported and selected by platform:
  Windows (DirectShow / dshow, video-only `capture.mp4`) and Linux (V4L2 + ALSA,
  `capture.mkv` with audio — e.g. a Raspberry Pi capturing from a C920). See
  [Recording video with ffmpeg](#recording-video-with-ffmpeg).

See `src/benchcam/recorders/` for the recorder code.

## Recording video with ffmpeg

The `ffmpeg` recorder captures one file into the session folder (**`capture.mp4`**
on Windows, **`capture.mkv`** on Linux). It starts when the session starts, so the
video timecode lines up with each marker's `elapsed_seconds`. ffmpeg is run as an
external binary — BenchCam adds no Python ffmpeg dependency, so you must have
`ffmpeg` on your `PATH` (`winget install Gyan.FFmpeg`, or download from
[ffmpeg.org](https://ffmpeg.org/download.html) and add it to `PATH`; on Debian/RPi
OS `sudo apt install ffmpeg`).

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

### 3. Recording headless on a Raspberry Pi (Linux / V4L2 + ALSA)

On Linux the recorder captures over **V4L2** and writes **`capture.mkv`**. The
C920 emits MJPEG natively, so the stream is **copied** (`-c:v copy`) — the Pi
never transcodes — and Matroska is used because MJPEG stream-copy is unreliable
in `.mp4`. ALSA audio (e.g. a Yeti Nano) is muxed into the same file.

The defaults match a confirmed C920 + Yeti Nano setup, so a plain session works
out of the box:

```bash
benchcam new --recorder ffmpeg --profile bench-pi
benchcam run        # captures /dev/video0 (mjpeg 1920x1080@30) + Yeti via ALSA
# ... benchcam mark "label" ...
benchcam end        # sends 'q' so capture.mkv is finalized
```

Everything is configurable (nothing is hardcoded in the command builder):

| Setting        | Default                   | Override |
| -------------- | ------------------------- | -------- |
| Video device   | `/dev/video0`             | `--camera` (stored in session.json) or `BENCHCAM_CAMERA` |
| Audio device   | `plughw:CARD=Nano,DEV=0`  | `--microphone` (stored in session.json) or `BENCHCAM_MICROPHONE` (set empty to disable audio) |
| Input format   | `mjpeg`                   | `BENCHCAM_INPUT_FORMAT` |
| Resolution     | `1920x1080`               | `BENCHCAM_VIDEO_SIZE` (`WxH`) |
| Frame rate     | `30`                      | `BENCHCAM_FRAMERATE` |

Discover V4L2 devices with `v4l2-ctl --list-devices` and ALSA capture devices
with `arecord -L`. The underlying command is equivalent to the manually
validated:

```bash
ffmpeg -f v4l2 -input_format mjpeg -framerate 30 -video_size 1920x1080 \
  -i /dev/video0 -f alsa -ac 2 -ar 44100 -i plughw:CARD=Nano,DEV=0 \
  -c:v copy -c:a aac capture.mkv
```

> Harmless `Dequeued v4l2 buffer contains corrupted data` warnings at stream
> startup are expected; they are logged to `ffmpeg.log` and are not fatal.

> macOS note: the avfoundation input path is still a TODO stub in
> `build_ffmpeg_command`.

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

- The stretches between events are **timelapsed** (default `--speed 30` for 30x).
- Around each marker the clip drops to **normal speed** for a window — default
  `--pre 3` seconds before and `--post 5` seconds after. Overlapping or adjacent
  windows merge into one normal-speed segment.
- With **`--vad`**, actual **speech** (detected by silero-VAD) also drives normal
  speed, so a timelapse never cuts you off mid-sentence and a chapter never flips
  before you start talking. A marker still forces a normal-speed window even when
  silent.
- **MUTE spans** (from `mute_spans.csv`, written by the MUTE button) are
  **force-timelapsed** — the mirror of a marker — even over speech, so asides you
  don't want in the video compress into the montage.
- **Audio** is kept full-precision in the normal-speed windows (your narration)
  and dropped in the timelapsed stretches (no chipmunk audio).
- Each chapter's **label** renders as a persistent on-screen **chapter tag**
  (top-left, monospace) that stays from its marker until the next chapter begins —
  a running "where am I now" indicator, drawn across timelapse segments too, not
  just a brief caption.
- If the capture is **unfinalized/headerless** (a card-full or hard-killed
  session with no duration header), `edit` does a one-time lossless remux to a
  sidecar and proceeds instead of refusing.

It reads the session's `capture.*` and `markers.csv` and writes `review.mp4` into
the **same** session folder. It needs `ffmpeg` (and `ffprobe`, which ships with
ffmpeg) on your `PATH` — see [Recording video with ffmpeg](#recording-video-with-ffmpeg)
for install instructions.

**One-command pipeline.** `benchcam edit --auto` orchestrates full-session
**transcribe → autochapter → render** in a single command, skipping any expensive
step whose output already exists (so re-rendering after a title tweak does zero
Whisper/API work). `--preview` writes a fast 720p `review.preview.mp4` for
iterating on chapter titles without a full render, and `--fetch` pulls the session
from the Pi first. These compose with `--vad`/`--speed`.

```powershell
# Edit the newest session with the defaults (3s pre / 5s post / 30x):
benchcam edit

# ...or a specific session, with custom pacing:
benchcam edit --session 2026-06-18_05-43-00 --pre 2 --post 6 --speed 12

# Custom caption font (optional):
benchcam edit --font "C:\Windows\Fonts\arial.ttf"
```

Chapter tags are rendered by ffmpeg's `drawtext`. BenchCam escapes the text and
the font path for ffmpeg's filtergraph automatically (so Windows paths like
`C:\Windows\Fonts\consola.ttf` and labels containing `:`, `'`, `,`, `\`, or `%`
render correctly), and passes the filtergraph via a temp filterscript. Tags
default to a **monospace** "engineering-terminal" font (Consolas on Windows,
DejaVu Sans Mono on Linux) for the aligned look; if the chosen `--font` isn't
found it falls back to a known system monospace font, and finally to ffmpeg's
default — the render never dies on a missing font. Pass `--font` to override.

`benchcam edit` first prints the **segment plan** (which stretches are timelapsed
vs. normal speed, and which captions land where) so you can sanity-check the
pacing before watching, then renders `sessions\<id>\review.mp4`. Re-running
overwrites `review.mp4` cleanly; `capture.*` is never modified or deleted.

Notes:

- **No markers** → a straight `--speed` timelapse of the whole video (still a
  quick way to skim a session); `edit` says so.
- Marker times past the end of the video are clamped to its length.
- The review is **timelapsed**, so a marker's raw `elapsed_seconds` is **not** its
  timestamp in `review.mp4`. `edit` prints the raw→review time map in its segment
  plan, and `benchcam publish` turns that map into a paste-ready YouTube chapter
  block — don't hand YouTube the raw marker times.
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
**Session library** with per-session actions. It calls the same BenchCam logic as
the CLI — start = `new` + `run`, mark = `mark`, stop = recorder stop + collect +
`end`, review = `edit`. If OBS isn't running, starting an OBS session shows a
clear error rather than silently failing; a second start is refused; stopping
when nothing is active is a no-op with a message.

**Naming sessions:** the Start screen has an optional **Session name** field.
When set, the session folder becomes `<timestamp>_<slug>` (e.g.
`2026-06-18_14-41-31_moteus-first-spin`) — the timestamp prefix is kept for
sorting/uniqueness — and the friendly name is stored in `session.json` so the UI
shows it nicely. With no name, the folder is timestamp-only as before; older
sessions without a name fall back to showing their folder name.

**Session library:** a list of every session under the sessions root (newest
first) showing the name, date/time, marker count, length, and whether a
`review.mp4` exists. Per row you can **Open video** (the capture in your default
player), **Make review** / **Open review** (render with the `pre`/`post`/`speed`
controls, or open the existing one), **Open folder** (in the file explorer), and
rename the session inline. Renaming an **ended** session **renames the folder on
disk** to `<original-timestamp>_<new-slug>` (the timestamp prefix is kept) and
updates `session.json` (and repoints `obs_recording.txt`/`.active` if needed), so
File Explorer reflects the new name. The currently recording session can't be
renamed — stop it first. The library just scans the session folders — there's no
separate database.

> The dashboard's MARK button is a click convenience. For hands-busy marking,
> `benchcam live` in a terminal (single keypress per marker) is still the fastest
> path — the dashboard does not replace it.

With the OBS recorder, **OBS itself is your live camera preview** next to the
dashboard; BenchCam just drives OBS's record start/stop so markers stay aligned.

The recorder dropdown defaults to the sensible choice for the machine: **ffmpeg**
on Linux (the Raspberry Pi capture box) and **OBS** on Windows — so you don't
have to pick it from the dropdown every time. Every recorder is still selectable.

### Use it from your phone (LAN access)

By default the dashboard binds to `127.0.0.1`, so it's reachable only from the
machine it runs on. To open it from a **phone on the same Wi-Fi** (e.g. standing
at the bench by the Pi), bind it to the LAN — this is **opt-in** because the
dashboard has **no authentication**:

```bash
# On the Pi (headless): expose on the LAN, don't try to open a local browser.
benchcam dashboard --lan --no-browser
# Equivalent: benchcam dashboard --host 0.0.0.0
# Or set it once:  export BENCHCAM_DASHBOARD_HOST=0.0.0.0
```

On startup it prints the actual phone-reachable URLs (the Pi's LAN IP and its
`http://<hostname>.local:8765/` mDNS name) — open one of those on your phone. It
never prints the bare `0.0.0.0` wildcard. **Only enable LAN access on a network
you trust**, since anyone on that network can reach the (unauthenticated) UI.

### OBS password (no env var needed)

You don't need to set `BENCHCAM_OBS_PASSWORD` before launching. When the OBS
recorder is selected, the Start screen shows an **OBS WebSocket password** field
(and optional host/port). Enter the password from OBS's **Tools → WebSocket
Server Settings → Show Connect Info** once and start a session — BenchCam saves
it to a **local, git-ignored** file at `.benchcam\config.json` (under the project
folder), so you won't have to re-enter it on future launches (leave the field
blank to reuse the saved one). The password resolves in order:

1. the field you typed on the Start screen,
2. the saved `.benchcam\config.json`,
3. the `BENCHCAM_OBS_PASSWORD` environment variable,

and if none is available the start fails with the same clear "authentication
enabled but no password provided" error as the CLI. The `.benchcam\` folder (and
`.env`) are git-ignored, so the password is never committed. Host/port work the
same way (defaulting to `localhost:4455`).

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
   preview, and make sure its WebSocket server is enabled (see
   [Recording video with OBS](#recording-video-with-obs)).
2. Double-click the desktop shortcut → the dashboard opens in your browser.
3. Pick the recorder (OBS by default), optionally type a profile and a
   **Session name**, and — the first time — paste the OBS WebSocket password into
   the **OBS WebSocket password** field (it's saved locally afterward). Click
   **Start session**; the indicator turns red **● RECORDING**.
4. Work at the bench: click **MARK now** for quick markers, or type a label and
   click **Mark + label**; add free-form notes with the note field. Markers
   appear in the live list with their elapsed time.
5. Click **Stop session** → the indicator returns to **○ IDLE** and the session
   appears at the top of the **Session library**.
6. In the library row, click **Make review** → BenchCam renders the marker-aware
   review clip into the session folder, opens it, and the row flips to **Open
   review** for next time. **Open video** plays the raw capture.

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
    cli.py            argparse CLI (new/run/mark/live/end/edit/transcribe/
                      label/autochapter/chapters/publish/fetch/dashboard)
    session.py        session model + on-disk layout
    markers.py        markers.csv reading/writing (incl. the narration column)
    live.py           interactive single-keypress marking shell
    keypress.py       cross-platform single-key reader (msvcrt / termios)
    transcribe.py     Whisper narration -> the marker `narration` column ([transcribe])
    label.py          narration -> terse technical label via the Claude API ([label])
    autochapter.py    full-session transcript -> faithful source=auto chapters
    chapters.py       review / re-title chapters (chapters.txt) without re-watching
    editor.py         marker-aware auto-edit -> review.mp4 (timelapse, VAD, chapter tags)
    publish.py        export a YouTube chapter block from the review remap
    dashboard.py      local web UI (stdlib http.server) over the existing logic
    config.py         local gitignored config (.benchcam/config.json; OBS creds)
    clock.py          time helpers
    recorders/
        base.py       Recorder interface
        null.py       NullRecorder (default)
        obs.py        ObsRecorder (OBS Studio via OBS WebSocket v5)
        ffmpeg.py     FfmpegRecorder (ffmpeg subprocess; Linux V4L2/ALSA + Windows dshow)
        collect.py    move an external recording into the session folder
scripts/
    benchcam-dashboard.vbs   no-console Windows launcher (recommended; pinnable)
    benchcam-dashboard.bat   Windows launcher (keeps a console window)
    benchcam-dashboard.ps1   PowerShell launcher
control-board/        v1 control PCB: KiCad source + the gerbers/mill gcode it
                      was cut from (built and verified, NOT yet working)
gpio_buttons.py       Pi-only GPIO daemon: buttons, record/mute LEDs, OLED
                      status panel, mute spans, ring light
reviewer.py           laptop review hub (list / open / watch / make-review)
tests/                unit tests
```

## License

[MIT](LICENSE) © 2026 Harrison Powe.
