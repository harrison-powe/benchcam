# BenchCam session format (v0)

This document describes the exact on-disk format BenchCam v0 writes. Everything
is **plain local files** that you can open in any text editor. There is **no
cloud sync**.

A small, hand-inspectable example lives in
[`examples/example_session/`](../examples/example_session/).

## Layout

Each session is a folder under the sessions root (default: `./sessions`):

```
sessions/
    .active                       pointer to the active session (folder name)
    YYYY-MM-DD_HH-MM-SS/          one folder per session
        session.json              session metadata
        markers.csv               time-stamped markers
        notes.md                  free-form operator notes
        artifacts.csv             manifest of attached media
        media/                    media copied into the session (copy mode)
```

The folder name is the session's local creation time formatted as
`YYYY-MM-DD_HH-MM-SS`. If two sessions are created in the same second, a numeric
suffix is added (e.g. `..._2`).

## session.json

UTF-8 JSON object. Wall-clock times are ISO 8601 strings in local time (with UTC
offset).

| Field | Type | Description |
| --- | --- | --- |
| `session_id` | string | Stable id; equals the session folder name. |
| `created_wall_time` | string | ISO 8601 time the session was created. |
| `profile` | string | Profile name (free text; default `"default"`). |
| `camera` | string | Camera description/device (may be empty). |
| `microphone` | string | Microphone description/device (may be empty). |
| `recorder` | string | Recorder backend: `null`, `obs`, or `ffmpeg`. |
| `storage_path` | string | Path to the session folder. |
| `notes` | string | Initial notes text passed at creation (may be empty). |
| `status` | string | Lifecycle state: `created`, `running`, or `ended`. |
| `started_wall_time` | string \| null | ISO 8601 time `benchcam run` was called; `null` until then. |
| `ended_wall_time` | string \| null | ISO 8601 time `benchcam end` was called; `null` until then. |

Example:

```json
{
  "session_id": "2026-06-17_05-43-00",
  "created_wall_time": "2026-06-17T05:43:00.123456+00:00",
  "profile": "bench-a",
  "camera": "",
  "microphone": "",
  "recorder": "null",
  "storage_path": "sessions/2026-06-17_05-43-00",
  "notes": "",
  "status": "ended",
  "started_wall_time": "2026-06-17T05:43:10.000000+00:00",
  "ended_wall_time": "2026-06-17T05:45:30.000000+00:00"
}
```

## markers.csv

Standard CSV with a header row, UTF-8 encoded. Values are quoted by Python's
`csv` module when needed (e.g. labels/notes containing commas).

| Column | Description |
| --- | --- |
| `marker_index` | 1-based index, increasing within the session. |
| `elapsed_seconds` | Seconds since `benchcam run` (creation time if not yet run), 3 decimals. |
| `wall_time` | ISO 8601 local timestamp of the marker. |
| `source` | Where the marker came from: `manual` (from `benchcam mark`) or another value for external feeds. |
| `label` | Free-text label. |
| `note` | Optional free-text note; empty string when omitted. |

Example:

```csv
marker_index,elapsed_seconds,wall_time,source,label,note
1,2.500,2026-06-17T05:43:12.500000+00:00,manual,power on,
2,15.250,2026-06-17T05:43:25.250000+00:00,manual,first motion,actuator moved after wiring fix
```

## notes.md

A free-form Markdown file for whatever the operator wants to jot down during the
session. BenchCam seeds it with a heading and otherwise leaves it to you. The
interactive `note <text>` command (see `benchcam run --interactive`) appends one
timestamped line per note in this simple format:

```markdown
- [2026-06-17T12:34:56-07:00] swapped encoder cable before retry
```

## artifacts.csv

A manifest of media files attached to the session via `benchcam attach-media`.
BenchCam does not capture video in v0; you record manually (OBS, webcam, phone,
audio recorder) and attach the result. Standard CSV with a header row, UTF-8.

Exact header:

```
artifact_index,added_wall_time,kind,label,original_path,stored_path,size_bytes,mode
```

| Column | Description |
| --- | --- |
| `artifact_index` | 1-based index, increasing within the session. |
| `added_wall_time` | ISO 8601 local timestamp the file was attached. |
| `kind` | `video`, `audio`, `image`, or `other`. |
| `label` | Optional user label; empty string when omitted. |
| `original_path` | Absolute, resolved path of the source file. |
| `stored_path` | For copy mode, path relative to the session folder (e.g. `media/clip.mp4`); empty for reference mode. |
| `size_bytes` | Size of the source file in bytes. |
| `mode` | `copy` or `reference`. |

### media/ and copy vs reference

- **copy** (default): the source file is copied into `media/`, preserving its
  filename. If that name already exists, a suffix is appended (`clip.mp4`,
  `clip-1.mp4`, `clip-2.mp4`, ...) so nothing is overwritten. The original
  source file is never modified or deleted.
- **reference**: nothing is copied. Only a manifest row is written, with the
  `original_path` and an empty `stored_path`. Useful for very large files that
  should stay where the recorder wrote them.

`kind` is inferred from the file extension (case-insensitive) when `--kind` is
omitted: `.mp4/.mov/.mkv/.avi/.webm` → video, `.wav/.mp3/.m4a/.flac` → audio,
`.jpg/.jpeg/.png/.webp` → image, everything else → other.

Both `artifacts.csv` and `media/` are created for new sessions, and are created
lazily by `attach-media` for older sessions that predate this feature.

## sessions/.active

A tiny pointer file at the sessions root that holds the **folder name** of the
active session (a single line). It lets the separate `run`, `mark`, `end`, and
`status` CLI invocations agree on which session they operate on.

- `benchcam new` writes the new session's folder name into `.active`.
- `benchcam run`, `benchcam mark`, and `benchcam status` (without `--session`)
  read it to find the active session.
- `benchcam end` clears `.active` after closing the session, so a subsequent
  `benchcam status` with no `--session` reports that there is no active session.
  Use `benchcam status --session <path>` to inspect a closed session.

## Notes on media and recorders

- **Video/media files are never committed to Git.** The `sessions/` directory
  and common media extensions are listed in `.gitignore`.
- **`NullRecorder` is the default recorder and captures no media.** It exists so
  the session and marker workflow can be exercised and tested without a camera,
  OBS, or ffmpeg. The `obs` and `ffmpeg` backends are stubs in v0.
- BenchCam only observes and logs. It does **not** control moving hardware or
  actuators.
