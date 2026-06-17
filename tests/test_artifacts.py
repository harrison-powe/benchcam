"""Tests for the media/artifact attachment module."""

from __future__ import annotations

import pytest

from benchcam import artifacts as artifacts_mod
from benchcam import session as session_mod
from benchcam.artifacts import ArtifactError


def _make_session(tmp_path):
    return session_mod.create_session(root=tmp_path / "sessions")


def _make_source(tmp_path, name="clip.mp4", data=b"fake media data"):
    src = tmp_path / name
    src.write_bytes(data)
    return src


def test_new_session_has_media_dir_and_artifacts_csv(tmp_path):
    session = _make_session(tmp_path)
    assert session.media_dir.is_dir()
    assert session.artifacts_file.exists()
    with session.artifacts_file.open(encoding="utf-8") as fh:
        header = fh.readline().strip()
    assert header == ",".join(artifacts_mod.ARTIFACTS_FIELDNAMES)


def test_ensure_session_media_is_lazy(tmp_path):
    session = _make_session(tmp_path)
    # Simulate an older session without media/ + artifacts.csv.
    session.artifacts_file.unlink()
    for child in session.media_dir.iterdir():
        child.unlink()
    session.media_dir.rmdir()
    assert not session.media_dir.exists()
    assert not session.artifacts_file.exists()

    artifacts_mod.ensure_session_media(session)
    assert session.media_dir.is_dir()
    assert session.artifacts_file.exists()


def test_attach_copy_copies_file_and_writes_row(tmp_path):
    session = _make_session(tmp_path)
    src = _make_source(tmp_path, data=b"abc123")

    artifact = artifacts_mod.attach_media(session, src, label="main OBS recording")

    assert artifact.mode == "copy"
    assert artifact.stored_path == "media/clip.mp4"
    assert (session.media_dir / "clip.mp4").exists()
    assert (session.media_dir / "clip.mp4").read_bytes() == b"abc123"
    # Original is untouched.
    assert src.exists()

    rows = artifacts_mod.read_artifacts(session.artifacts_file)
    assert len(rows) == 1
    row = rows[0]
    assert row["artifact_index"] == "1"
    assert row["kind"] == "video"
    assert row["label"] == "main OBS recording"
    assert row["original_path"] == str(src.resolve())
    assert row["stored_path"] == "media/clip.mp4"
    assert row["size_bytes"] == "6"
    assert row["mode"] == "copy"


def test_attach_reference_does_not_copy(tmp_path):
    session = _make_session(tmp_path)
    src = _make_source(tmp_path)

    artifact = artifacts_mod.attach_media(session, src, mode="reference")

    assert artifact.mode == "reference"
    assert artifact.stored_path == ""
    assert list(session.media_dir.iterdir()) == []

    rows = artifacts_mod.read_artifacts(session.artifacts_file)
    assert rows[0]["mode"] == "reference"
    assert rows[0]["stored_path"] == ""
    assert rows[0]["original_path"] == str(src.resolve())


@pytest.mark.parametrize(
    "name,expected",
    [
        ("a.MP4", "video"),
        ("a.mov", "video"),
        ("a.webm", "video"),
        ("a.WAV", "audio"),
        ("a.mp3", "audio"),
        ("a.flac", "audio"),
        ("a.JPG", "image"),
        ("a.png", "image"),
        ("a.webp", "image"),
        ("a.txt", "other"),
        ("a", "other"),
    ],
)
def test_infer_kind(tmp_path, name, expected):
    from pathlib import Path

    assert artifacts_mod.infer_kind(Path(name)) == expected


def test_explicit_kind_overrides_inference(tmp_path):
    session = _make_session(tmp_path)
    src = _make_source(tmp_path, name="weird.dat")
    artifact = artifacts_mod.attach_media(session, src, kind="audio")
    assert artifact.kind == "audio"


def test_invalid_kind_raises(tmp_path):
    session = _make_session(tmp_path)
    src = _make_source(tmp_path)
    with pytest.raises(ArtifactError):
        artifacts_mod.attach_media(session, src, kind="hologram")


def test_invalid_mode_raises(tmp_path):
    session = _make_session(tmp_path)
    src = _make_source(tmp_path)
    with pytest.raises(ArtifactError):
        artifacts_mod.attach_media(session, src, mode="move")


def test_copy_collision_appends_suffix(tmp_path):
    session = _make_session(tmp_path)
    src = _make_source(tmp_path, name="example.mp4")

    a1 = artifacts_mod.attach_media(session, src)
    a2 = artifacts_mod.attach_media(session, src)
    a3 = artifacts_mod.attach_media(session, src)

    assert a1.stored_path == "media/example.mp4"
    assert a2.stored_path == "media/example-1.mp4"
    assert a3.stored_path == "media/example-2.mp4"
    assert (session.media_dir / "example.mp4").exists()
    assert (session.media_dir / "example-1.mp4").exists()
    assert (session.media_dir / "example-2.mp4").exists()


def test_missing_source_raises(tmp_path):
    session = _make_session(tmp_path)
    with pytest.raises(ArtifactError):
        artifacts_mod.attach_media(session, tmp_path / "nope.mp4")


def test_directory_source_raises(tmp_path):
    session = _make_session(tmp_path)
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(ArtifactError):
        artifacts_mod.attach_media(session, d)
