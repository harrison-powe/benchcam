# BenchCam

catching actuators in the act

BenchCam is a **local-first**, bench-side capture and marker logging tool for
hardware work. You start a session, log time-stamped markers as you work, and
everything is saved as plain local files you can read with any text editor.

## What BenchCam is (and is not)

- It records a session and logs markers (events with a timestamp + label).
- It keeps everything in local files only. **No cloud sync.**
- It can drive a recorder to capture video (ffmpeg works on Windows; OBS planned).
- It **does not** control any moving hardware or actuators. BenchCam may later
  receive external marker events, but it stays strictly on the observation side.

v0 ships with a **NullRecorder** (records no video) so you can use and test the
session + marker workflow immediately, with or without a camera. The
**FfmpegRecorder** records real video from a webcam on Windows (see
[Recording video with ffmpeg](#recording-video-with-ffmpeg)); the OBS backend is
still a stub.

## Requirements

- Python 3.11 or newer.
- For video recording: a working `ffmpeg` binary on your `PATH` (only needed if
  you use `--recorder ffmpeg`; the default `null` recorder needs nothing extra).

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

Useful options:

- `benchcam new --profile NAME --camera DESC --microphone DESC --recorder {null,obs,ffmpeg} --notes "..."`
- `benchcam mark "label" --source external`
- `--sessions-root PATH` (on any command) to use a different sessions directory.

The "active" session is tracked by a small pointer file at
`sessions\.active`, so `run`, `mark`, and `end` know which session to use.

## Recorders

- **NullRecorder** (`null`, default): records no video. Lets you exercise the
  session and marker workflow, and pairs well with capturing video manually in a
  separate app.
- **ObsRecorder** (`obs`): stub. Planned to drive OBS Studio via obs-websocket.
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
- Video/media files (including each session's `capture.mp4`) and the `sessions\`
  directory are **git-ignored** and must not be committed. Video is large —
  keep recordings on your external SSD, not in git.

## Project layout

```
src/benchcam/
    cli.py            argparse CLI (new/run/mark/live/end)
    session.py        session model + on-disk layout
    markers.py        markers.csv reading/writing
    live.py           interactive single-keypress marking shell
    keypress.py       cross-platform single-key reader (msvcrt / termios)
    clock.py          time helpers
    recorders/
        base.py       Recorder interface
        null.py       NullRecorder (default)
        obs.py        ObsRecorder stub
        ffmpeg.py     FfmpegRecorder (ffmpeg subprocess; Windows/dshow)
tests/                unit tests
```

## License

MIT.
