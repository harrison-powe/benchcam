"""ObsRecorder: drive OBS Studio's recording over OBS WebSocket v5.

OBS stays the live dashboard (preview / framing / focus); BenchCam just tells it
when to start and stop recording. Because BenchCam triggers the start, marker
``elapsed_seconds`` (measured from session start) lines up with the OBS video
timecode automatically.

Protocol (OBS WebSocket v5, bundled into OBS 28+, no separate plugin):
- Default server ``ws://localhost:4455``; password auth is on by default and uses
  a SHA256 challenge-response handshake.
- Request types are ``StartRecord`` / ``StopRecord`` / ``GetRecordStatus`` /
  ``GetVersion`` (the v4 ``StartRecording`` names do not work).
- ``StopRecord`` returns the output file path OBS wrote.

We use the ``obsws-python`` client rather than hand-rolling websocket framing or
the auth handshake. It is an OPTIONAL extra (``pip install benchcam[obs]``) and is
imported lazily inside :func:`_import_req_client`, so importing BenchCam without
the extra never fails. The video file lives wherever OBS is configured to record
(not the session folder); :meth:`ObsRecorder.stop` records that path as a sidecar
in the session folder.

Capture/observe only — never commands moving hardware.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .base import Recorder, RecorderError
from .collect import collect_recording

_LOG = logging.getLogger("benchcam.recorders.obs")

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 4455
DEFAULT_TIMEOUT = 3

ENV_HOST = "BENCHCAM_OBS_HOST"
ENV_PORT = "BENCHCAM_OBS_PORT"
ENV_PASSWORD = "BENCHCAM_OBS_PASSWORD"

#: Sidecar file (written into the session folder) holding the OBS output path.
RECORDING_POINTER_FILENAME = "obs_recording.txt"
NOTES_FILENAME = "notes.md"

_IMPORT_HINT = (
    "The OBS recorder needs the optional 'obsws-python' package. Install it with: "
    "pip install benchcam[obs]  — and make sure OBS Studio is running with its "
    "WebSocket server enabled (Tools -> WebSocket Server Settings)."
)


def _import_req_client():
    """Lazily import obsws-python's ReqClient, with a clear install hint."""
    try:
        import obsws_python as obsws  # noqa: PLC0415 - optional, imported on demand
    except ImportError as exc:
        raise RecorderError(_IMPORT_HINT) from exc
    return obsws.ReqClient


def _connect_refused_msg(host: str, port: int) -> str:
    return (
        f"Could not connect to OBS at {host}:{port} (connection refused). Is OBS "
        "Studio running with its WebSocket server enabled "
        "(Tools -> WebSocket Server Settings)?"
    )


def _connect_failed_msg(host: str, port: int, exc: Exception) -> str:
    return (
        f"Could not connect to OBS at {host}:{port}: {exc}. Check the port and "
        "password (set BENCHCAM_OBS_PASSWORD) and that the OBS WebSocket server "
        "is enabled."
    )


class ObsRecorder(Recorder):
    """Start/stop OBS Studio recording via OBS WebSocket v5."""

    name = "obs"

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        password: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        client_factory=None,
    ) -> None:
        # ``client_factory`` is an injection seam for tests; production resolves
        # the real ReqClient lazily so the import stays optional.
        self._host = host
        self._port = port
        self._password = password
        self._timeout = timeout
        self._client_factory = client_factory
        self._client = None
        self._storage_path: Path | None = None
        self._output_path: str | None = None

    @property
    def output_path(self) -> str | None:
        """Path OBS reported for the recording (available after stop())."""
        return self._output_path

    def _resolve_connection(self) -> tuple[str, int, str]:
        """Resolve host/port/password: constructor > env vars > defaults."""
        host = self._host if self._host is not None else os.environ.get(ENV_HOST, DEFAULT_HOST)
        if self._port is not None:
            port = int(self._port)
        else:
            port = int(os.environ.get(ENV_PORT, DEFAULT_PORT))
        password = (
            self._password
            if self._password is not None
            else os.environ.get(ENV_PASSWORD, "")
        )
        return host, port, password

    def start(self, storage_path: Path) -> None:
        self._storage_path = Path(storage_path)
        host, port, password = self._resolve_connection()

        factory = self._client_factory or _import_req_client()

        try:
            client = factory(
                host=host, port=port, password=password, timeout=self._timeout
            )
        except ConnectionRefusedError as exc:
            raise RecorderError(_connect_refused_msg(host, port)) from exc
        except RecorderError:
            raise
        except Exception as exc:
            raise RecorderError(_connect_failed_msg(host, port, exc)) from exc

        try:
            client.get_version()  # verify the request/response channel works
            status = client.get_record_status()
            if getattr(status, "output_active", False):
                raise RecorderError(
                    "OBS is already recording. Stop the current OBS recording "
                    "before starting a BenchCam session so the timecodes stay "
                    "aligned."
                )
            client.start_record()
        except RecorderError:
            self._safe_disconnect(client)
            raise
        except Exception as exc:
            self._safe_disconnect(client)
            raise RecorderError(f"Failed to start OBS recording: {exc}") from exc

        self._client = client

    def stop(self, storage_path: Path | None = None) -> None:
        client = self._client
        if client is None:
            return
        try:
            response = client.stop_record()
            output_path = getattr(response, "output_path", "") or ""
            if output_path:
                final_path = self._collect_into_session(output_path)
                self._record_output_path(final_path)
        except Exception:
            # Tolerate "already stopped" / transient errors so ending a session
            # never crashes. The recording (if any) is still safely in OBS.
            pass
        finally:
            self._safe_disconnect(client)
            self._client = None

    def _collect_into_session(self, output_path: str) -> str:
        """Move the OBS-recorded file into the session folder, if possible.

        OBS writes to its own folder, so on session end we move the video next to
        the markers as ``capture<ext>``. Returns the new in-folder path on
        success, or the original external path if collection is skipped or fails
        (so a failed collect degrades to a pointer, never to lost video).
        """
        if self._storage_path is None:
            return output_path
        collected = collect_recording(
            output_path, self._storage_path, warn=_LOG.warning
        )
        if collected is None:
            return output_path
        return str(collected)

    def _record_output_path(self, path: str) -> None:
        """Persist the OBS output path as a sidecar + a notes.md line.

        OBS writes the video to its own configured folder, not the session
        folder, so we leave a pointer in the session folder to correlate the two
        without expanding the session schema.
        """
        self._output_path = path
        if self._storage_path is None:
            return
        try:
            (self._storage_path / RECORDING_POINTER_FILENAME).write_text(
                path + "\n", encoding="utf-8"
            )
        except OSError:
            pass
        try:
            with (self._storage_path / NOTES_FILENAME).open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write(f"\nOBS recording: {path}\n")
        except OSError:
            pass

    @staticmethod
    def _safe_disconnect(client) -> None:
        disconnect = getattr(client, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception:
                pass
