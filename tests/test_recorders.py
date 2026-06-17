"""Tests for recorder backends."""

from __future__ import annotations

import pytest

from benchcam.recorders import NullRecorder, get_recorder
from benchcam.recorders.base import RecorderError


def test_null_recorder_start_stop(tmp_path):
    rec = NullRecorder()
    assert rec.is_running is False
    rec.start(tmp_path)
    assert rec.is_running is True
    rec.stop()
    assert rec.is_running is False


def test_get_recorder_by_name():
    assert isinstance(get_recorder("null"), NullRecorder)


def test_get_unknown_recorder_raises():
    with pytest.raises(RecorderError):
        get_recorder("does-not-exist")


@pytest.mark.parametrize("name", ["obs", "ffmpeg"])
def test_stub_recorders_start_raises(name, tmp_path):
    rec = get_recorder(name)
    with pytest.raises(RecorderError):
        rec.start(tmp_path)
    # stop() stays safe even on stubs
    rec.stop()
