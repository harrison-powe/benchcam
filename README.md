# BenchCam

Catching actuators in the act.

BenchCam is a **local-first**, bench-side capture and marker logging tool for
hardware work. You start a session, log time-stamped markers as you work, and
everything is saved as plain local files you can read with any text editor.

## What BenchCam is (and is not)

- It records a session and logs markers (events with a timestamp + label).
- It keeps everything in local files only. **No cloud sync.**
- It can drive a recorder (OBS / ffmpeg, planned) to capture video.
- It **does not** control any moving hardware or actuators. BenchCam may later
  receive external marker events, but it stays strictly on the observation side.

v0 ships with a **NullRecorder** (records no video) so you can use and test the
session + marker workflow immediately, with or without a camera. OBS and ffmpeg
backends are stubbed with clear TODOs.

## Requirements

- Python 3.11 or newer.

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

# Markers can carry an optional note for extra context
benchcam mark "first motion" --note "actuator moved after wiring fix"

# Check on the active session at any time
benchcam status

# Close the session
benchcam end
```

`benchcam status` prints a concise summary of the active session:

```text
session:    sessions\2026-06-17_05-43-00
session_id: 2026-06-17_05-43-00
status:     running
recorder:   null
profile:    bench-a
created:    2026-06-17T05:43:00.123456+00:00
started:    2026-06-17T05:43:10.000000+00:00
markers:    3
notes:      sessions\2026-06-17_05-43-00\notes.md
```

You can also inspect a specific session folder (handy after it has ended, since
`benchcam end` clears the active pointer):

```powershell
benchcam status --session sessions\2026-06-17_05-43-00
```

### Interactive mode (recommended for live bench work)

Running a separate command for every marker is awkward while your hands are
busy. `benchcam run --interactive` starts the session (if it is not already
running) and opens a simple line-based prompt so you can log markers and notes
without leaving the terminal:

```powershell
benchcam new
benchcam run --interactive
m first motion | actuator moved after wiring fix
note swapped encoder cable
status
end
```

Inside the prompt:

| Input | What it does |
| --- | --- |
| `m <label>` | Add a marker (source `keyboard`, empty note). |
| `m <label> \| <note>` | Add a marker with a note. |
| `note <text>` | Append a timestamped line to `notes.md`. |
| `status` | Print the session status summary. |
| `help` | List the available commands. |
| `q`, `quit`, or `end` | End the session, stop the recorder, and exit. |
| (blank line) | Ignored. |

This is **line-based terminal input** for v0 (type a command, press Enter). It is
not physical GPIO buttons or hotkeys yet — that comes with later Raspberry Pi
support. BenchCam only logs the events; it never drives moving hardware.

### Attaching media recorded elsewhere

BenchCam does not capture video in v0. Record manually (OBS, a webcam app, your
phone, an audio recorder, ...) and then attach the file to the session:

```powershell
benchcam new
benchcam run --interactive
# ...record manually in OBS if desired, log markers/notes...
end
benchcam attach-media "C:/path/to/obs-recording.mp4" --label "main OBS recording"
```

- **Copy mode is the default**: the file is copied into `<session>\media\` and a
  row is added to `<session>\artifacts.csv`. Copy mode never overwrites an
  existing file — it appends `-1`, `-2`, ... to the name if needed.
- **Reference mode** (`--mode reference`) avoids copying large files; it records
  the original path in `artifacts.csv` and leaves the file wherever it is (handy
  for big OBS recordings). The original source file is never modified or deleted.
- The kind (`video`/`audio`/`image`/`other`) is inferred from the extension, or
  set it explicitly with `--kind`.
- Attach to a specific session with `--session <path>` (works even after the
  session has ended).

Media lives under the git-ignored `sessions\` directory and **must not be
committed**.

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
marker_index,elapsed_seconds,wall_time,source,label,note
1,2.500,2026-06-17T05:43:12.500000+00:00,manual,power on,
2,15.250,2026-06-17T05:43:25.250000+00:00,manual,first motion,actuator moved after wiring fix
```

- `elapsed_seconds` is measured from when you ran `benchcam run`.
- `source` is `manual` for `benchcam mark`; a future external feed can use other
  sources.
- `note` is an optional free-text note (empty string when omitted).

### notes.md

A free-form Markdown file for whatever you want to jot down during the session.

## CLI reference

| Command | What it does |
| --- | --- |
| `benchcam new` | Create a new session folder and make it the active session. |
| `benchcam run` | Start recording / start the session clock. Add `--interactive` for a line-based marker/note prompt. |
| `benchcam mark "label"` | Append a time-stamped marker to the active session. |
| `benchcam end` | Stop recording and close the active session. |
| `benchcam status` | Print a summary of the active session (or `--session PATH`). |
| `benchcam attach-media <file>` | Attach an externally recorded file to a session (copy by default). |

Useful options:

- `benchcam new --profile NAME --camera DESC --microphone DESC --recorder {null,obs,ffmpeg} --notes "..."`
- `benchcam mark "label" --source external --note "extra context"`
- `benchcam status --session sessions\2026-06-17_05-43-00`
- `benchcam attach-media FILE --label "..." --kind video --mode {copy,reference} --session PATH`
- `--sessions-root PATH` (on any command) to use a different sessions directory.

The "active" session is tracked by a small pointer file at `sessions\.active`,
so `run`, `mark`, `end`, and `status` know which session to use. `benchcam end`
clears that pointer once the session is closed, so a fresh `benchcam status`
with no `--session` reports that there is no active session. The full format is
documented in [docs/session-format.md](docs/session-format.md), and a small
hand-inspectable example lives in
[examples/example_session/](examples/example_session/).

## Recorders

- **NullRecorder** (`null`, default): records no video. Lets you exercise the
  session and marker workflow, and pairs well with capturing video manually in a
  separate app.
- **ObsRecorder** (`obs`): stub. Planned to drive OBS Studio via obs-websocket.
- **FfmpegRecorder** (`ffmpeg`): stub. Planned to capture via an `ffmpeg`
  subprocess.

See `src/benchcam/recorders/` for the stub TODOs.

## Development

```powershell
pip install -e ".[dev]"
pytest
```

## Data and privacy

- BenchCam writes only to your local `sessions\` directory.
- Video/media files and the `sessions\` directory are **git-ignored** and must
  not be committed.

## Project layout

```
src/benchcam/
    cli.py            argparse CLI (new/run/mark/status/end/attach-media)
    session.py        session model + on-disk layout
    markers.py        markers.csv reading/writing
    artifacts.py      media attachment + artifacts.csv manifest
    clock.py          time helpers
    recorders/
        base.py       Recorder interface
        null.py       NullRecorder (default)
        obs.py        ObsRecorder stub
        ffmpeg.py     FfmpegRecorder stub
    inputs/
        keyboard_input.py  line-based interactive marker loop
tests/                unit tests
```

## License

MIT.
