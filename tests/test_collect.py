"""Tests for collect_recording (moving an external video into the session folder).

No real OBS/ffmpeg; cross-drive behavior is simulated by patching os.replace so
no second filesystem is needed.
"""

from __future__ import annotations

from unittest import mock

from benchcam.recorders import collect as collect_mod
from benchcam.recorders.collect import collect_recording


def _make_file(path, content=b"video-bytes"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_collect_moves_file_into_session_and_returns_dest(tmp_path):
    src = _make_file(tmp_path / "obs_out" / "2026-06-18.mkv")
    session = tmp_path / "sessions" / "S1"
    session.mkdir(parents=True)

    dest = collect_recording(src, session)

    assert dest == session / "capture.mkv"
    assert dest.exists()
    assert dest.read_bytes() == b"video-bytes"
    assert not src.exists()  # moved, not copied


def test_collect_preserves_extension(tmp_path):
    src = _make_file(tmp_path / "obs_out" / "clip.mp4")
    session = tmp_path / "sessions" / "S2"
    session.mkdir(parents=True)

    dest = collect_recording(src, session)
    assert dest == session / "capture.mp4"


def test_collect_waits_then_succeeds(tmp_path):
    src = tmp_path / "obs_out" / "late.mkv"  # does not exist yet
    session = tmp_path / "sessions" / "S3"
    session.mkdir(parents=True)

    calls = {"n": 0}

    def fake_sleep(_interval):
        # OBS "finalizes" the file after the first poll.
        calls["n"] += 1
        if calls["n"] == 1:
            _make_file(src)

    dest = collect_recording(
        src, session, wait_timeout=2.0, poll_interval=0.1, sleep=fake_sleep
    )

    assert dest == session / "capture.mkv"
    assert dest.exists()
    assert calls["n"] >= 1  # it had to wait at least once


def test_collect_gives_up_when_file_never_appears(tmp_path):
    src = tmp_path / "obs_out" / "never.mkv"
    session = tmp_path / "sessions" / "S4"
    session.mkdir(parents=True)
    warnings: list[str] = []

    dest = collect_recording(
        src,
        session,
        wait_timeout=0.3,
        poll_interval=0.1,
        sleep=lambda _i: None,
        warn=warnings.append,
    )

    assert dest is None
    assert warnings  # warned about the missing file
    assert not (session / "capture.mkv").exists()


def test_collect_move_failure_keeps_source_and_returns_none(tmp_path):
    src = _make_file(tmp_path / "obs_out" / "locked.mkv")
    session = tmp_path / "sessions" / "S5"
    session.mkdir(parents=True)
    warnings: list[str] = []

    with mock.patch.object(
        collect_mod.os, "replace", side_effect=OSError("rename failed")
    ), mock.patch.object(
        collect_mod.shutil, "copy2", side_effect=OSError("copy failed")
    ):
        dest = collect_recording(src, session, warn=warnings.append)

    assert dest is None
    assert src.exists()  # source preserved -> no data loss
    assert not (session / "capture.mkv").exists()  # partial cleaned up
    assert warnings


def test_collect_cross_drive_uses_copy_then_delete(tmp_path):
    src = _make_file(tmp_path / "obs_out" / "xdrive.mkv")
    session = tmp_path / "sessions" / "S6"
    session.mkdir(parents=True)

    # Simulate a cross-filesystem rename failing, forcing the copy+delete path.
    with mock.patch.object(
        collect_mod.os, "replace", side_effect=OSError(18, "Invalid cross-device link")
    ), mock.patch.object(
        collect_mod.shutil, "copy2", wraps=collect_mod.shutil.copy2
    ) as copy2, mock.patch.object(
        collect_mod.os, "remove", wraps=collect_mod.os.remove
    ) as remove:
        dest = collect_recording(src, session)

    assert dest == session / "capture.mkv"
    assert dest.exists()
    copy2.assert_called_once()
    remove.assert_called_once()
    assert not src.exists()


def test_collect_skips_when_already_in_session_folder(tmp_path):
    # Represents an in-folder recorder (like ffmpeg's capture.mp4): no double-move.
    session = tmp_path / "sessions" / "S7"
    session.mkdir(parents=True)
    in_folder = _make_file(session / "capture.mp4")

    dest = collect_recording(in_folder, session)

    assert dest == in_folder
    assert in_folder.exists()
    assert in_folder.read_bytes() == b"video-bytes"


def test_collect_none_source_returns_none(tmp_path):
    assert collect_recording("", tmp_path) is None
    assert collect_recording(None, tmp_path) is None
