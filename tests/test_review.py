"""Tests for the Markdown session review export."""

from __future__ import annotations

from benchcam import review as review_mod
from benchcam import session as session_mod


def _make_session(tmp_path):
    return session_mod.create_session(root=tmp_path / "sessions", profile="bench-a")


def test_build_review_has_all_sections(tmp_path):
    session = _make_session(tmp_path)
    text = review_mod.build_review(session)

    assert text.startswith("# BenchCam Session Review")
    assert "## Session" in text
    assert "## Markers" in text
    assert "## Artifacts" in text
    assert "## Notes" in text
    assert "## Review Checklist" in text
    assert "- [ ] Identify key moments worth clipping or referencing." in text
    assert session.session_id in text
    assert session.storage_path in text


def test_write_review_default_path(tmp_path):
    session = _make_session(tmp_path)
    out = review_mod.write_review(session)
    assert out == session.folder / "review.md"
    assert out.exists()
    assert out.read_text(encoding="utf-8").startswith("# BenchCam Session Review")


def test_write_review_custom_output(tmp_path):
    session = _make_session(tmp_path)
    target = tmp_path / "exports" / "nested" / "review-out.md"
    out = review_mod.write_review(session, target)
    assert out == target
    assert target.exists()
    # Default review.md is NOT written when --output is used.
    assert not (session.folder / "review.md").exists()


def test_markers_table_includes_note(tmp_path):
    session = _make_session(tmp_path)
    session_mod.start_session(session)
    session_mod.add_marker(session, "first motion", note="actuator moved")

    text = review_mod.build_review(session)
    assert "| Index | Elapsed seconds | Wall time | Source | Label | Note |" in text
    assert "first motion" in text
    assert "actuator moved" in text


def test_marker_label_and_note_with_pipe_are_escaped(tmp_path):
    session = _make_session(tmp_path)
    session_mod.start_session(session)
    session_mod.add_marker(session, "a | b", note="c | d")

    text = review_mod.build_review(session)
    # Raw unescaped pipes from the data must not appear; escaped form must.
    assert "a \\| b" in text
    assert "c \\| d" in text
    # Every table row should have a consistent number of cell separators.
    marker_rows = [
        ln
        for ln in text.splitlines()
        if ln.startswith("| ") and "a \\| b" in ln
    ]
    assert len(marker_rows) == 1
    # 6 columns -> 7 pipe separators (escaped pipes use backslash-pipe).
    assert marker_rows[0].count(" | ") == 5


def test_artifacts_table_includes_copy_and_reference(tmp_path):
    from benchcam import artifacts as artifacts_mod

    session = _make_session(tmp_path)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")
    big = tmp_path / "big.mkv"
    big.write_bytes(b"x" * 50)

    artifacts_mod.attach_media(session, src, label="copied", mode="copy")
    artifacts_mod.attach_media(session, big, label="referenced", mode="reference")

    text = review_mod.build_review(session)
    assert "| Index | Kind | Label | Mode | Stored path | Original path | Size bytes |" in text
    assert "copied" in text
    assert "referenced" in text
    assert "media/clip.mp4" in text
    # Both modes are represented as cell values.
    assert "| copy |" in text
    assert "| reference |" in text


def test_empty_markers_message(tmp_path):
    session = _make_session(tmp_path)
    text = review_mod.build_review(session)
    assert "No markers recorded." in text


def test_empty_artifacts_message(tmp_path):
    session = _make_session(tmp_path)
    text = review_mod.build_review(session)
    assert "No artifacts attached." in text


def test_empty_notes_message(tmp_path):
    session = _make_session(tmp_path)
    # Overwrite the seeded notes with whitespace only.
    session.notes_file.write_text("   \n\n", encoding="utf-8")
    text = review_mod.build_review(session)
    assert "No notes recorded." in text


def test_notes_contents_included(tmp_path):
    session = _make_session(tmp_path)
    session.notes_file.write_text("# heading\n\nsome observation\n", encoding="utf-8")
    text = review_mod.build_review(session)
    assert "some observation" in text


def test_review_does_not_modify_source_artifacts(tmp_path):
    session = _make_session(tmp_path)
    session_mod.start_session(session)
    session_mod.add_marker(session, "m1")
    before = {
        "session": session.session_file.read_bytes(),
        "markers": session.markers_file.read_bytes(),
        "artifacts": session.artifacts_file.read_bytes(),
        "notes": session.notes_file.read_bytes(),
    }
    review_mod.write_review(session)
    assert session.session_file.read_bytes() == before["session"]
    assert session.markers_file.read_bytes() == before["markers"]
    assert session.artifacts_file.read_bytes() == before["artifacts"]
    assert session.notes_file.read_bytes() == before["notes"]


def test_started_and_ended_times_appear_when_present(tmp_path):
    session = _make_session(tmp_path)
    text_before = review_mod.build_review(session)
    assert "Started wall time" not in text_before
    assert "Ended wall time" not in text_before

    session_mod.start_session(session)
    session_mod.end_session(session)
    text_after = review_mod.build_review(session)
    assert "Started wall time" in text_after
    assert "Ended wall time" in text_after
