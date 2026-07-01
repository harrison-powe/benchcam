"""Tests for AI narration->label summarization (benchcam label).

The narration->label decision (plan_targets) and the output sanitizer
(sanitize_label) are pure and are the core of these tests. The Anthropic API is
never called for real — summarize_narration and make_client are monkeypatched,
so no key and no network are needed.
"""

from __future__ import annotations

import pytest

from benchcam import label as label_mod
from benchcam.markers import read_markers
from benchcam.label import (
    DEFAULT_MODEL,
    ENV_API_KEY,
    ENV_MODEL,
    LabelError,
    plan_targets,
    resolve_model,
    run_label,
    sanitize_label,
)


# --------------------------------------------------------------------------- #
# sanitize_label (pure)
# --------------------------------------------------------------------------- #

def test_sanitize_strips_trailing_period():
    assert sanitize_label("Power Connected.") == "Power Connected"


def test_sanitize_preserves_internal_period_in_version():
    # The whole point: "R4.11" must not become "R4".
    assert sanitize_label("Flashing R4.11") == "Flashing R4.11"
    assert sanitize_label("Flashing R4.11.") == "Flashing R4.11"


def test_sanitize_strips_surrounding_quotes():
    assert sanitize_label('"Power Connected"') == "Power Connected"
    assert sanitize_label("'Reflow Start'") == "Reflow Start"


def test_sanitize_collapses_whitespace_and_takes_first_line():
    assert sanitize_label("  Chip   Lifted  ") == "Chip Lifted"
    assert sanitize_label("Power Connected\nsome stray second line") == "Power Connected"


def test_sanitize_strips_other_trailing_punctuation_not_internal():
    assert sanitize_label("Fault Detected!") == "Fault Detected"
    assert sanitize_label("Verifying Moteus Enable:") == "Verifying Moteus Enable"


def test_sanitize_caps_runaway_response():
    long = "one two three four five six seven eight nine"
    assert sanitize_label(long) == "one two three four five six"


def test_sanitize_empty():
    assert sanitize_label("") == ""
    assert sanitize_label(None) == ""


# --------------------------------------------------------------------------- #
# plan_targets (which markers get labeled)
# --------------------------------------------------------------------------- #

def _markers(*rows):
    # Each row is (narration, label, source).
    out = []
    for i, (narration, label, source) in enumerate(rows, start=1):
        out.append(
            {
                "marker_index": str(i),
                "elapsed_seconds": f"{i}.000",
                "wall_time": "2026-06-22T12:00:00",
                "source": source,
                "label": label,
                "narration": narration,
            }
        )
    return out


def test_plan_only_markers_with_narration():
    markers = _markers(
        ("gonna connect the power", "", "gpio+transcribed"),  # -> labeled
        ("", "", "gpio"),  # no narration -> nothing to summarize -> skipped
    )
    targets = plan_targets(markers)
    assert [(t.marker_index, t.narration) for t in targets] == [
        (1, "gonna connect the power")
    ]


def test_plan_preserves_hand_typed_label_without_overwrite():
    markers = _markers(("gonna connect the power", "Power On", "gpio+transcribed"))
    assert plan_targets(markers) == []  # label already set -> preserved


def test_plan_overwrite_relabels_existing_label():
    markers = _markers(("gonna connect the power", "Power On", "gpio+transcribed"))
    targets = plan_targets(markers, overwrite=True)
    assert [t.marker_index for t in targets] == [1]


def test_plan_tags_source_preserving_origin():
    markers = _markers(("gonna connect the power", "", "gpio+transcribed"))
    assert plan_targets(markers)[0].source == "gpio+transcribed+ai-labeled"


def test_plan_source_tag_is_idempotent():
    markers = _markers(("x", "", "gpio+transcribed+ai-labeled"))
    targets = plan_targets(markers, overwrite=True)
    assert targets[0].source == "gpio+transcribed+ai-labeled"


def test_plan_ignores_unparseable_rows():
    markers = [{"marker_index": "x", "narration": "hi", "label": "", "source": "m"}]
    assert plan_targets(markers) == []


# --------------------------------------------------------------------------- #
# Model resolution
# --------------------------------------------------------------------------- #

def test_resolve_model_flag_wins(monkeypatch):
    monkeypatch.setenv(ENV_MODEL, "claude-env")
    assert resolve_model("claude-flag") == "claude-flag"


def test_resolve_model_env_fallback(monkeypatch):
    monkeypatch.setenv(ENV_MODEL, "claude-env")
    assert resolve_model(None) == "claude-env"


def test_resolve_model_default(monkeypatch):
    monkeypatch.delenv(ENV_MODEL, raising=False)
    assert resolve_model(None) == DEFAULT_MODEL


# --------------------------------------------------------------------------- #
# make_client / missing dependency + key (no SDK, no network)
# --------------------------------------------------------------------------- #

def test_make_client_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    with pytest.raises(LabelError) as exc:
        label_mod.make_client()
    assert ENV_API_KEY in str(exc.value)


def test_make_client_missing_sdk_raises(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, "sk-ant-test")

    def boom():
        raise LabelError(label_mod._INSTALL_HINT)

    monkeypatch.setattr(label_mod, "_import_anthropic", boom)
    with pytest.raises(LabelError) as exc:
        label_mod.make_client()
    assert "label" in str(exc.value).lower()


# --------------------------------------------------------------------------- #
# Orchestration (API mocked)
# --------------------------------------------------------------------------- #

def _session_with_narration(tmp_path, *rows):
    """Create a real session and write markers with (narration, label, source)."""
    from benchcam import session as session_mod
    from benchcam.markers import FIELDNAMES
    import csv

    root = tmp_path / "sessions"
    session = session_mod.create_session(root=root)
    with session.markers_file.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for i, (narration, label, source) in enumerate(rows, start=1):
            writer.writerow(
                {
                    "marker_index": i,
                    "elapsed_seconds": f"{i}.000",
                    "wall_time": "2026-06-22T12:00:00",
                    "source": source,
                    "label": label,
                    "narration": narration,
                }
            )
    return session


def _fake_summaries(monkeypatch, mapping):
    monkeypatch.setattr(label_mod, "make_client", lambda: object())
    # Return the model's *raw* text (with a trailing period to prove sanitizing).
    monkeypatch.setattr(
        label_mod,
        "summarize_narration",
        lambda client, narration, model: mapping[narration.strip()],
    )


def test_run_label_writes_label_and_tags_source(tmp_path, monkeypatch):
    session = _session_with_narration(
        tmp_path, ("gonna connect the power", "", "gpio+transcribed")
    )
    _fake_summaries(monkeypatch, {"gonna connect the power": "Power Connected."})

    results = run_label(session.folder, out=lambda _m: None)
    assert [(r.marker_index, r.label) for r in results] == [(1, "Power Connected")]
    written = read_markers(session.markers_file)
    assert written[0]["label"] == "Power Connected"  # sanitized (period stripped)
    assert written[0]["narration"] == "gonna connect the power"  # untouched
    assert written[0]["source"] == "gpio+transcribed+ai-labeled"


def test_run_label_dry_run_writes_nothing(tmp_path, monkeypatch):
    session = _session_with_narration(
        tmp_path, ("gonna connect the power", "", "gpio+transcribed")
    )
    _fake_summaries(monkeypatch, {"gonna connect the power": "Power Connected"})

    results = run_label(session.folder, dry_run=True, out=lambda _m: None)
    assert [r.label for r in results] == ["Power Connected"]
    written = read_markers(session.markers_file)
    assert written[0]["label"] == ""  # nothing written in dry run
    assert written[0]["source"] == "gpio+transcribed"  # source untouched


def test_run_label_no_markers(tmp_path):
    from benchcam import session as session_mod

    session = session_mod.create_session(root=tmp_path / "sessions")
    session.markers_file.unlink()  # no markers.csv at all
    messages: list[str] = []
    assert run_label(session.folder, out=messages.append) == []
    assert any("no markers" in m.lower() for m in messages)


def test_run_label_no_narration_to_summarize(tmp_path, monkeypatch):
    session = _session_with_narration(tmp_path, ("", "", "gpio"))
    monkeypatch.setattr(label_mod, "make_client", lambda: object())
    messages: list[str] = []
    assert run_label(session.folder, out=messages.append) == []
    assert any("narration" in m.lower() for m in messages)


def test_cli_label_dispatches(tmp_path, monkeypatch):
    from benchcam.cli import main

    session = _session_with_narration(
        tmp_path, ("gonna connect the power", "", "gpio+transcribed")
    )
    _fake_summaries(monkeypatch, {"gonna connect the power": "Power Connected"})

    code = main(
        [
            "label",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--session",
            str(session.folder),
        ]
    )
    assert code == 0
    assert read_markers(session.markers_file)[0]["label"] == "Power Connected"
