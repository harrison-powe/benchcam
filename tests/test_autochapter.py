"""Tests for benchcam autochapter.

The pure guarantees (citation validation, merge/conflict, response parsing) are
tested directly; Whisper (``transcribe_audio``) and the Anthropic client
(``make_client``) are mocked so no model runs and no network call is made.

Load-bearing tests (hard requirements):
- ``test_no_chapter_invented_in_dead_air`` — a fabricated chapter in a silent gap
  cannot cite real transcript text, so validation drops it.
- ``test_existing_gpio_manual_win_over_conflicting_auto`` — real markers are
  preserved and win over a conflicting auto chapter.
"""

from __future__ import annotations

from benchcam import autochapter as ac
from benchcam import session as session_mod
from benchcam.autochapter import (
    AUTO_SOURCE,
    ProposedChapter,
    ValidChapter,
    merge_chapters,
    parse_chapters_response,
    validate_chapters,
)
from benchcam.markers import read_markers
from benchcam.transcribe import TranscriptSegment as TS


# --------------------------------------------------------------------------- #
# Fake Anthropic client (mirrors the SDK's response shape)
# --------------------------------------------------------------------------- #

class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeClient:
    """Returns a fixed JSON payload from messages.create, like the real client."""

    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kwargs):
            self._outer.calls.append(kwargs)
            return _Response(self._outer._payload)

    @property
    def messages(self):
        return _FakeClient._Messages(self)


# --------------------------------------------------------------------------- #
# 1. Anti-hallucination: nothing is invented in a dead-air gap (HARD REQUIREMENT)
# --------------------------------------------------------------------------- #

def test_no_chapter_invented_in_dead_air():
    # Narration exists at 0-705s and again at 980s, with a SILENT gap 780-960s.
    segments = [
        TS(10, 14, "starting the stiffness test on the arm"),
        TS(120, 126, "okay configuring the spin parameters now"),
        TS(700, 705, "first spin looks good it is actually turning"),
        # --- dead air 780-960s: no segments to cite ---
        TS(980, 986, "velocity sweep running across the range now"),
    ]
    proposed = [
        # legit (time will snap to the cited segment start)
        ProposedChapter(0, "stiffness test", 999.0, "Stiffness Test"),
        ProposedChapter(2, "first spin", 700.0, "First Spin"),
        # hallucinated in the gap: cites an out-of-range segment index
        ProposedChapter(99, "dead air event", 870.0, "Ghost Chapter"),
        # hallucinated in the gap: cites a real segment but the quote is fabricated
        ProposedChapter(3, "calibrating the gyro bias", 870.0, "Phantom Event"),
    ]

    valid = validate_chapters(proposed, segments)
    titles = [c.title for c in valid]

    # Both fabricated chapters are dropped...
    assert "Ghost Chapter" not in titles
    assert "Phantom Event" not in titles
    # ...and NOTHING survives inside the 780-960s dead-air gap.
    assert all(not (780 < c.at_seconds < 960) for c in valid)
    # The real, transcript-supported chapters survive, snapped to their cite time.
    assert "Stiffness Test" in titles and "First Spin" in titles
    stiffness = next(c for c in valid if c.title == "Stiffness Test")
    assert stiffness.at_seconds == 10  # snapped to segment start, not the model's 999


# --------------------------------------------------------------------------- #
# 2. Additive/non-destructive: real markers win on conflict (HARD REQUIREMENT)
# --------------------------------------------------------------------------- #

def _row(index, seconds, source, label):
    return {
        "marker_index": str(index),
        "elapsed_seconds": f"{seconds:.3f}",
        "wall_time": "",
        "source": source,
        "label": label,
        "narration": "",
    }


def test_existing_gpio_manual_win_over_conflicting_auto():
    existing = [
        _row(1, 300.0, "gpio", "Button A"),
        _row(2, 600.0, "manual", "Typed Note"),
    ]
    chapters = [
        ValidChapter(at_seconds=305.0, title="Near Gpio", quote="a", segment=0),
        ValidChapter(at_seconds=610.0, title="Near Manual", quote="b", segment=1),
        ValidChapter(at_seconds=1000.0, title="Clear Event", quote="c", segment=2),
    ]

    plan = merge_chapters(existing, chapters, conflict_window=20.0)

    # Only the non-conflicting chapter is written.
    assert [c.title for c in plan.new_auto] == ["Clear Event"]
    # Both real markers are preserved, verbatim, and untouched.
    assert len(plan.protected) == 2
    assert plan.protected[0]["source"] == "gpio"
    assert plan.protected[0]["label"] == "Button A"
    assert plan.protected[1]["source"] == "manual"
    assert plan.protected[1]["label"] == "Typed Note"


# --------------------------------------------------------------------------- #
# 3. Snap-to-cite + title sanitize
# --------------------------------------------------------------------------- #

def test_validate_chapters_snaps_at_to_cited_segment():
    segments = [TS(40, 45, "okay I am starting the stiffness test now")]
    proposed = [
        # Model put at_seconds nowhere near the cite; title has trailing punctuation.
        ProposedChapter(0, "starting the stiffness test", 999.0, "Stiffness Test."),
    ]
    valid = validate_chapters(proposed, segments)

    assert len(valid) == 1
    assert valid[0].at_seconds == 40  # snapped to the cited segment's start
    assert valid[0].title == "Stiffness Test"  # sanitize_label stripped the period


def test_validate_chapters_tolerates_off_by_one_cite():
    # The quote really belongs to segment 1 but the model cited segment 0; the
    # +-1 neighbor search still resolves it (and snaps to the matched segment).
    segments = [
        TS(10, 15, "first we power everything on"),
        TS(30, 36, "now the CAN link just dropped out"),
    ]
    proposed = [ProposedChapter(0, "CAN link just dropped", 30.0, "CAN Link Lost")]
    valid = validate_chapters(proposed, segments)
    assert len(valid) == 1
    assert valid[0].at_seconds == 30  # matched + snapped to segment 1
    assert valid[0].title == "CAN Link Lost"  # acronym casing preserved (no .title())


# --------------------------------------------------------------------------- #
# 4. Tolerant response parsing
# --------------------------------------------------------------------------- #

def test_parse_chapters_response_tolerates_fences_and_prose():
    text = (
        "Here are the chapters I found:\n"
        "```json\n"
        '[{"segment": 0, "quote": "first spin", "at_seconds": 12.5, '
        '"title": "First Spin"}]\n'
        "```\n"
        "Let me know if you want more."
    )
    parsed = parse_chapters_response(text)
    assert len(parsed) == 1
    assert parsed[0].segment == 0
    assert parsed[0].title == "First Spin"
    assert parsed[0].at_seconds == 12.5


def test_parse_chapters_response_handles_junk():
    assert parse_chapters_response("no json here") == []
    assert parse_chapters_response("") == []


# --------------------------------------------------------------------------- #
# 5. Orchestration: caches transcript, writes auto markers, reuses cache
# --------------------------------------------------------------------------- #

def _patch_pipeline(monkeypatch, segments, payload):
    """Mock Whisper + ffmpeg + the Anthropic client; return the transcribe spy."""
    calls = {"transcribe": 0}

    def fake_transcribe(capture, model, *, language="en"):
        calls["transcribe"] += 1
        return segments

    monkeypatch.setattr(ac, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(ac, "probe_has_audio", lambda *a, **k: True)
    monkeypatch.setattr(ac, "find_capture", lambda d: d / "capture.mkv")
    monkeypatch.setattr(ac.shutil, "which", lambda _n: "/usr/bin/x")
    monkeypatch.setattr(ac, "make_client", lambda: _FakeClient(payload))
    return calls


_SEGMENTS = [
    TS(10, 15, "okay starting the stiffness test now"),
    TS(300, 306, "and now the velocity sweep is running"),
]
_PAYLOAD = (
    '[{"segment": 0, "quote": "starting the stiffness test", "at_seconds": 10, '
    '"title": "Stiffness Test"},'
    ' {"segment": 1, "quote": "velocity sweep is running", "at_seconds": 300, '
    '"title": "Velocity Sweep"}]'
)


def test_run_autochapter_caches_transcript_and_writes_auto_markers(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mkv").write_bytes(b"video")

    # Ensure ffmpeg 'import shutil' inside _transcribe_full sees a present binary.
    calls = _patch_pipeline(monkeypatch, _SEGMENTS, _PAYLOAD)

    written = ac.run_autochapter(session.folder, out=lambda _m: None)

    assert [c.title for c in written] == ["Stiffness Test", "Velocity Sweep"]
    assert ac.transcript_path(session.folder).exists()  # cached
    rows = read_markers(session.markers_file)
    auto = [r for r in rows if r["source"] == AUTO_SOURCE]
    assert [r["label"] for r in auto] == ["Stiffness Test", "Velocity Sweep"]
    assert [r["elapsed_seconds"] for r in auto] == ["10.000", "300.000"]
    assert calls["transcribe"] == 1

    # Re-run without --overwrite: the cache is reused (no second transcription)
    # and the auto chapters are regenerated in place, not duplicated.
    ac.run_autochapter(session.folder, out=lambda _m: None)
    assert calls["transcribe"] == 1  # cache reused
    rows2 = read_markers(session.markers_file)
    assert len([r for r in rows2 if r["source"] == AUTO_SOURCE]) == 2  # not doubled


def test_run_autochapter_whisper_model_mismatch_re_transcribes(tmp_path, monkeypatch):
    # A cached transcript from a DIFFERENT Whisper model is not reused: the
    # provenance stamp makes it a miss, so the requested model re-transcribes
    # and replaces the cache.
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mkv").write_bytes(b"video")
    ac.save_transcript(session.folder, _SEGMENTS, model="tiny", language="en")

    calls = _patch_pipeline(monkeypatch, _SEGMENTS, _PAYLOAD)
    ac.run_autochapter(session.folder, whisper_model="small", out=lambda _m: None)

    assert calls["transcribe"] == 1  # stale-model cache ignored -> fresh pass
    assert ac.load_cached_transcript(
        session.folder, model="small", language="en"
    ) == _SEGMENTS  # cache replaced, restamped with the requested model


def test_run_autochapter_preserves_real_marker_in_written_file(tmp_path, monkeypatch):
    # File-level proof of the merge rule: a real gpio marker survives untouched
    # and a conflicting auto chapter is dropped when the file is rewritten.
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mkv").write_bytes(b"video")
    # A real press 5s from the first proposed chapter (10s) -> auto one dropped.
    session_mod.add_marker(session, "Real Press", source="gpio")
    import benchcam.markers as m
    m.update_marker(session.markers_file, 1, {"elapsed_seconds": "12.000"})

    _patch_pipeline(monkeypatch, _SEGMENTS, _PAYLOAD)

    ac.run_autochapter(session.folder, conflict_window=20.0, out=lambda _m: None)

    rows = read_markers(session.markers_file)
    gpio = [r for r in rows if r["source"] == "gpio"]
    auto = [r for r in rows if r["source"] == AUTO_SOURCE]
    # The real marker is preserved exactly...
    assert len(gpio) == 1 and gpio[0]["label"] == "Real Press"
    # ...the Stiffness Test chapter (10s, within 20s of the 12s press) is dropped,
    # only the far Velocity Sweep survives.
    assert [r["label"] for r in auto] == ["Velocity Sweep"]


# --------------------------------------------------------------------------- #
# 6. Dry run writes no markers (but does cache the transcript)
# --------------------------------------------------------------------------- #

def test_run_autochapter_dry_run_writes_no_markers(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    (session.folder / "capture.mkv").write_bytes(b"video")

    _patch_pipeline(monkeypatch, _SEGMENTS, _PAYLOAD)

    proposed = ac.run_autochapter(session.folder, dry_run=True, out=lambda _m: None)

    assert [c.title for c in proposed] == ["Stiffness Test", "Velocity Sweep"]
    # markers.csv gained NO auto rows...
    rows = read_markers(session.markers_file)
    assert not any(r["source"] == AUTO_SOURCE for r in rows)
    # ...but the transcript WAS cached (so the real run is instant).
    assert ac.transcript_path(session.folder).exists()
