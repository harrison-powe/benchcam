"""Tests for session creation and lifecycle."""

from __future__ import annotations

import json

import pytest

from benchcam import session as session_mod
from benchcam.session import SessionError, slugify


def test_create_session_creates_folder_and_files(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root, profile="bench-a")

    assert session.folder.exists()
    assert session.session_file.exists()
    assert session.markers_file.exists()
    assert session.notes_file.exists()
    assert session.session_id == session.folder.name


def test_session_json_has_required_fields(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(
        root=root,
        profile="p1",
        camera="cam0",
        microphone="mic0",
        recorder="null",
        notes="hello",
    )

    data = json.loads(session.session_file.read_text(encoding="utf-8"))
    for field in [
        "session_id",
        "created_wall_time",
        "profile",
        "camera",
        "microphone",
        "recorder",
        "storage_path",
        "notes",
    ]:
        assert field in data, f"missing field {field}"

    assert data["profile"] == "p1"
    assert data["camera"] == "cam0"
    assert data["microphone"] == "mic0"
    assert data["recorder"] == "null"
    assert data["notes"] == "hello"


def test_create_session_sets_active(tmp_path):
    root = tmp_path / "sessions"
    created = session_mod.create_session(root=root)
    active = session_mod.get_active_session(root)
    assert active.session_id == created.session_id


def test_get_active_without_session_raises(tmp_path):
    root = tmp_path / "sessions"
    with pytest.raises(SessionError):
        session_mod.get_active_session(root)


def test_unique_folder_when_timestamp_collides(tmp_path, monkeypatch):
    root = tmp_path / "sessions"

    fixed = "2026-01-02_03-04-05"
    monkeypatch.setattr(
        session_mod.clock, "folder_timestamp", lambda dt: fixed
    )

    a = session_mod.create_session(root=root)
    b = session_mod.create_session(root=root)

    assert a.folder != b.folder
    assert a.folder.name == fixed
    assert b.folder.name == f"{fixed}_2"


def test_start_and_end_session_updates_status(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    assert session.status == session_mod.STATUS_CREATED

    session_mod.start_session(session)
    assert session.status == session_mod.STATUS_RUNNING
    assert session.started_wall_time is not None

    reloaded = session_mod.load_session(session.folder)
    assert reloaded.status == session_mod.STATUS_RUNNING

    session_mod.end_session(session)
    assert session.status == session_mod.STATUS_ENDED
    assert session.ended_wall_time is not None


def test_slugify_handles_spaces_unsafe_chars_and_empty():
    assert slugify("Moteus First Spin") == "moteus-first-spin"
    assert slugify("  Bad/Chars: #1!! ") == "badchars-1"
    assert slugify("a   b") == "a-b"
    assert slugify("---") == ""
    assert slugify("") == ""
    assert slugify(None) == ""


def test_create_session_named_folder_and_name_field(tmp_path, monkeypatch):
    monkeypatch.setattr(
        session_mod.clock, "folder_timestamp", lambda dt: "2026-06-18_14-41-31"
    )
    s = session_mod.create_session(root=tmp_path / "sessions", name="Moteus First Spin!")
    assert s.session_id == "2026-06-18_14-41-31_moteus-first-spin"
    assert s.folder.name == s.session_id
    assert s.name == "Moteus First Spin!"        # human-readable stored
    assert s.display_name == "Moteus First Spin!"
    # name persisted to session.json
    data = json.loads(s.session_file.read_text(encoding="utf-8"))
    assert data["name"] == "Moteus First Spin!"


def test_create_session_unnamed_is_timestamp_only(tmp_path, monkeypatch):
    monkeypatch.setattr(
        session_mod.clock, "folder_timestamp", lambda dt: "2026-06-18_14-41-31"
    )
    s = session_mod.create_session(root=tmp_path / "sessions")
    assert s.session_id == "2026-06-18_14-41-31"  # no slug appended
    assert s.name == ""
    assert s.display_name == "2026-06-18_14-41-31"


def test_load_old_session_without_name_falls_back_to_folder(tmp_path):
    root = tmp_path / "sessions"
    s = session_mod.create_session(root=root)
    # Simulate an older session.json that predates the name field.
    data = json.loads(s.session_file.read_text(encoding="utf-8"))
    data.pop("name", None)
    s.session_file.write_text(json.dumps(data), encoding="utf-8")

    loaded = session_mod.load_session(s.folder)
    assert loaded.name == ""
    assert loaded.display_name == loaded.session_id


def test_cannot_start_ended_session(tmp_path):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    session_mod.end_session(session)
    with pytest.raises(SessionError):
        session_mod.start_session(session)
