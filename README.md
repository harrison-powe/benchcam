# BenchCam

catching actuators in the act

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
benchcam mark "fault observed"

# Close the session
benchcam end
```

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
    cli.py            argparse CLI (new/run/mark/end)
    session.py        session model + on-disk layout
    markers.py        markers.csv reading/writing
    clock.py          time helpers
    recorders/
        base.py       Recorder interface
        null.py       NullRecorder (default)
        obs.py        ObsRecorder stub
        ffmpeg.py     FfmpegRecorder stub
tests/                unit tests
```

## License

MIT.
