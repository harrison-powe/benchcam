"""Tests for ObsRecorder.

No real OBS and no real network: the obsws-python client is replaced with an
injected fake via ``client_factory``. These tests do NOT require the optional
``obsws-python`` extra to be installed (the import-error path is exercised by
forcing the import to fail), so the suite stays green in a stdlib-only env.
"""

from __future__ import annotations

import sys

import pytest

from benchcam.recorders import get_recorder
from benchcam.recorders.base import RecorderError
from benchcam.recorders.obs import (
    ENV_PASSWORD,
    ENV_PORT,
    RECORDING_POINTER_FILENAME,
    ObsRecorder,
)


class FakeResp:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeClient:
    """Stand-in for obsws_python.ReqClient that records calls."""

    def __init__(self, *, output_active=False, stop_path="C:\\OBS\\rec.mkv"):
        self.calls: list[str] = []
        self.conn: dict = {}
        self._output_active = output_active
        self._stop_path = stop_path

    def get_version(self):
        self.calls.append("get_version")
        return FakeResp(obs_version="30.0.0")

    def get_record_status(self):
        self.calls.append("get_record_status")
        return FakeResp(output_active=self._output_active)

    def start_record(self):
        self.calls.append("start_record")

    def stop_record(self):
        self.calls.append("stop_record")
        return FakeResp(output_path=self._stop_path)

    def disconnect(self):
        self.calls.append("disconnect")


def _factory_for(client, captured=None):
    def factory(**conn):
        client.conn = conn
        if captured is not None:
            captured.update(conn)
        return client

    return factory


def _clear_obs_env(monkeypatch):
    for var in ("BENCHCAM_OBS_HOST", ENV_PORT, ENV_PASSWORD):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------- #
# start()
# --------------------------------------------------------------------------- #

def test_start_connects_then_sends_start_record(tmp_path, monkeypatch):
    _clear_obs_env(monkeypatch)
    client = FakeClient()
    ObsRecorder(client_factory=_factory_for(client)).start(tmp_path)

    assert "start_record" in client.calls
    assert "get_version" in client.calls
    # Status is checked before recording is started.
    assert client.calls.index("get_record_status") < client.calls.index("start_record")


def test_start_resolves_connection_from_env(tmp_path, monkeypatch):
    _clear_obs_env(monkeypatch)
    monkeypatch.setenv(ENV_PASSWORD, "s3cret")
    monkeypatch.setenv(ENV_PORT, "4499")
    captured: dict = {}
    client = FakeClient()
    ObsRecorder(client_factory=_factory_for(client, captured)).start(tmp_path)

    assert captured["password"] == "s3cret"
    assert captured["port"] == 4499
    assert captured["host"] == "localhost"


def test_constructor_password_overrides_env(tmp_path, monkeypatch):
    _clear_obs_env(monkeypatch)
    monkeypatch.setenv(ENV_PASSWORD, "from-env")
    captured: dict = {}
    client = FakeClient()
    ObsRecorder(password="from-ctor", client_factory=_factory_for(client, captured)).start(
        tmp_path
    )
    assert captured["password"] == "from-ctor"


def test_start_raises_if_obs_already_recording(tmp_path, monkeypatch):
    _clear_obs_env(monkeypatch)
    client = FakeClient(output_active=True)
    rec = ObsRecorder(client_factory=_factory_for(client))

    with pytest.raises(RecorderError) as exc:
        rec.start(tmp_path)

    assert "already recording" in str(exc.value).lower()
    assert "start_record" not in client.calls  # did not start a second recording
    assert "disconnect" in client.calls  # cleaned up the connection


def test_connection_refused_raises_clear_error(tmp_path, monkeypatch):
    _clear_obs_env(monkeypatch)

    def factory(**conn):
        raise ConnectionRefusedError(111, "Connection refused")

    rec = ObsRecorder(client_factory=factory)
    with pytest.raises(RecorderError) as exc:
        rec.start(tmp_path)

    msg = str(exc.value).lower()
    assert "obs" in msg
    assert "running" in msg or "refused" in msg


def test_missing_obsws_python_raises_install_hint(tmp_path, monkeypatch):
    _clear_obs_env(monkeypatch)
    # Force `import obsws_python` to fail even if the extra happens to be present.
    monkeypatch.setitem(sys.modules, "obsws_python", None)

    rec = ObsRecorder()  # no client_factory -> takes the lazy-import path
    with pytest.raises(RecorderError) as exc:
        rec.start(tmp_path)

    assert "benchcam[obs]" in str(exc.value)


# --------------------------------------------------------------------------- #
# stop()
# --------------------------------------------------------------------------- #

def test_stop_sends_stop_record_and_captures_output_path(tmp_path, monkeypatch):
    _clear_obs_env(monkeypatch)
    client = FakeClient(stop_path="D:\\Recordings\\bench.mkv")
    rec = ObsRecorder(client_factory=_factory_for(client))
    rec.start(tmp_path)
    rec.stop()

    assert "stop_record" in client.calls
    assert "disconnect" in client.calls
    assert rec.output_path == "D:\\Recordings\\bench.mkv"

    sidecar = (tmp_path / RECORDING_POINTER_FILENAME).read_text(encoding="utf-8")
    assert "D:\\Recordings\\bench.mkv" in sidecar


def test_stop_tolerates_already_stopped(tmp_path, monkeypatch):
    _clear_obs_env(monkeypatch)

    class RaisingStop(FakeClient):
        def stop_record(self):
            self.calls.append("stop_record")
            raise RuntimeError("output not active")

    client = RaisingStop()
    rec = ObsRecorder(client_factory=_factory_for(client))
    rec.start(tmp_path)
    rec.stop()  # must not raise

    assert "disconnect" in client.calls


def test_stop_is_safe_when_never_started():
    ObsRecorder().stop()  # no client; must not raise


def test_get_recorder_obs_returns_instance():
    assert isinstance(get_recorder("obs"), ObsRecorder)
