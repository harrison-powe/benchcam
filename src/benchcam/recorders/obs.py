"""ObsRecorder: stub for driving OBS Studio.

This backend is NOT implemented yet. The intended approach is to control a
running OBS instance over the obs-websocket protocol (OBS 28+ ships it built in)
so BenchCam can start/stop recording without managing encoders itself.

TODO (OBS backend):
- TODO: Add an optional dependency on an obs-websocket client
  (for example ``obsws-python``) under an ``[obs]`` extra.
- TODO: Read connection settings (host, port, password) from a profile/config
  rather than hard-coding them.
- TODO: On start(), connect to obs-websocket and send StartRecord.
- TODO: On stop(), send StopRecord and disconnect cleanly.
- TODO: Capture the output file path OBS reports and record it in session.json.
- TODO: Fail clearly if OBS is not running or the websocket is unreachable.
- TODO: Never send any command that could move hardware; recording control only.
"""

from __future__ import annotations

from pathlib import Path

from .base import Recorder, RecorderError


class ObsRecorder(Recorder):
    """Placeholder recorder for OBS Studio (obs-websocket)."""

    name = "obs"

    def start(self, storage_path: Path) -> None:
        raise RecorderError(
            "ObsRecorder is not implemented yet. Use the 'null' recorder for v0. "
            "See benchcam/recorders/obs.py for the planned implementation."
        )

    def stop(self) -> None:
        # Stopping a recorder that never started is a no-op so end/cleanup paths
        # stay safe even when the OBS backend is selected by mistake.
        return None
