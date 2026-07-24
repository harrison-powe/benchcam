"""CLI prefix-collision register - the permanent guard from the
--session -> --sessions-root incident.

argparse abbreviation (allow_abbrev=True, the default everywhere) silently
expands any typed option that is a proper prefix of exactly ONE of a command's
options. When the typed string is real vocabulary on ANOTHER command
(--session, --overwrite, --lan), that expansion is a silent trap: the command
runs with an option the user never meant. Two were harmful and are now closed
by REAL options that exact-match first:

    chapters/publish --session   (was: silently set the sessions ROOT to the
                                  session id; now a real --session alias)
    edit --overwrite             (was: silently became --overwrite-auto and
                                  triggered an unrequested Whisper pass +
                                  chapter regeneration; now a guard option
                                  that errors with guidance)

The register below is the full REVIEWED surface of the silent expansions that
remain. The test computes the actual surface by parser introspection and
asserts EQUALITY with the register: any new option that creates a new silent
expansion - or removes/changes a registered one - fails the suite and forces
a human decision here, recorded with a disposition comment.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from benchcam import cli as cli_mod
from benchcam import session as session_mod
from benchcam.cli import main

_SILENT_EXPANSION_REGISTER = {
    # ---- --session on commands with NO session concept (severity S3) -------
    # Silently retargets the sessions ROOT (e.g. 'new --session X' creates a
    # session under root X). Reviewed 2026-07-24 and DECLINED to guard: six
    # dummy erroring options would be worse than the disease. A NEW command
    # appearing in this class must be a deliberate decision, not an oversight.
    ("new", "--session"): "--sessions-root",
    ("run", "--session"): "--sessions-root",
    ("mark", "--session"): "--sessions-root",
    ("end", "--session"): "--sessions-root",
    ("live", "--session"): "--sessions-root",
    ("dashboard", "--session"): "--sessions-root",
    # ---- --session on commands with a REQUIRED positional (severity S4) ----
    # The expansion itself is silent, but the run then fails LOUDLY on the
    # missing required positional (fetch: session, merge: sessions). If either
    # positional ever becomes optional, that loud failure turns into a silent
    # wrong-root run - these entries are the tripwire to re-review then.
    ("fetch", "--session"): "--sessions-root",
    ("merge", "--session"): "--sessions-root",
    # ---- --lan (dashboard vocabulary) on Whisper commands (severity S5) ----
    # Expands to --language, which then fails loudly ("expected one argument")
    # - a mislabeled but loud error. Accepted.
    ("transcribe", "--lan"): "--language",
    ("autochapter", "--lan"): "--language",
}


def _subparsers():
    parser = cli_mod.build_parser()
    return next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )


def _silent_expansion_surface():
    """Every (command, typed-option) -> unique-expansion pair across the CLI.

    For each long option V that exists on ANY command, and each command C that
    does NOT itself define V: if V is a proper prefix of exactly one of C's
    options, argparse expands it silently. (Two or more matches error loudly
    as ambiguous; zero matches error as unrecognized - neither is silent, so
    neither is in scope.)
    """
    subs = _subparsers()
    options = {
        name: {
            s for act in sp._actions for s in act.option_strings if s.startswith("--")
        }
        for name, sp in subs.choices.items()
    }
    vocabulary = sorted(set().union(*options.values()))
    surface = {}
    for name, opts in options.items():
        for typed in vocabulary:
            if typed in opts:
                continue
            matches = [o for o in opts if o.startswith(typed)]
            if len(matches) == 1:
                surface[(name, typed)] = matches[0]
    return surface


def test_silent_prefix_expansions_match_reviewed_register():
    surface = _silent_expansion_surface()
    new = {k: v for k, v in surface.items() if k not in _SILENT_EXPANSION_REGISTER}
    gone = {k: v for k, v in _SILENT_EXPANSION_REGISTER.items() if k not in surface}
    assert not new, (
        "NEW silent prefix expansion(s) introduced - close each with a real "
        f"option/guard, or review and register it with a disposition: {new}"
    )
    assert not gone, (
        "Registered silent expansion(s) no longer exist - if that was a "
        f"deliberate fix, remove them from the register: {gone}"
    )
    assert surface == _SILENT_EXPANSION_REGISTER


def test_fixed_traps_stay_fixed():
    # The two harmful traps must stay closed: chapters/publish define a real
    # --session and edit a real --overwrite, so exact typing can never
    # prefix-expand again.
    surface = _silent_expansion_surface()
    assert ("chapters", "--session") not in surface
    assert ("publish", "--session") not in surface
    assert ("edit", "--overwrite") not in surface


# --------------------------------------------------------------------------- #
# --session alias behavior on chapters/publish (+ the edit --overwrite guard)
# --------------------------------------------------------------------------- #

def _capture_session_dir(monkeypatch, module, func_name):
    seen: list[Path] = []

    def fake(session_dir, **kwargs):
        seen.append(Path(session_dir))
        return 0

    monkeypatch.setattr(module, func_name, fake)
    return seen


def _one_session(tmp_path):
    root = tmp_path / "sessions"
    return root, session_mod.create_session(root=root)


def test_chapters_session_flag_selects_that_session(tmp_path, monkeypatch):
    root, session = _one_session(tmp_path)
    seen = _capture_session_dir(monkeypatch, cli_mod.chapters_mod, "run_chapters")

    rc = main(["chapters", "--sessions-root", str(root), "--session", session.session_id])

    assert rc == 0
    assert seen and seen[0].name == session.session_id


def test_chapters_positional_still_works(tmp_path, monkeypatch):
    root, session = _one_session(tmp_path)
    seen = _capture_session_dir(monkeypatch, cli_mod.chapters_mod, "run_chapters")

    rc = main(["chapters", "--sessions-root", str(root), session.session_id])

    assert rc == 0
    assert seen and seen[0].name == session.session_id


def test_chapters_both_forms_error_even_when_equal(tmp_path, monkeypatch, capsys):
    root, session = _one_session(tmp_path)
    seen = _capture_session_dir(monkeypatch, cli_mod.chapters_mod, "run_chapters")

    rc = main([
        "chapters", "--sessions-root", str(root),
        session.session_id, "--session", session.session_id,  # equal values
    ])

    assert rc == 1
    assert seen == []  # never reached the command
    err = capsys.readouterr().err
    assert "once" in err and "--session" in err


def test_publish_session_flag_selects_that_session(tmp_path, monkeypatch):
    root, session = _one_session(tmp_path)
    seen = _capture_session_dir(monkeypatch, cli_mod.publish_mod, "run_publish")

    rc = main(["publish", "--sessions-root", str(root), "--session", session.session_id])

    assert rc == 0
    assert seen and seen[0].name == session.session_id


def test_publish_both_forms_error(tmp_path, monkeypatch, capsys):
    root, session = _one_session(tmp_path)
    seen = _capture_session_dir(monkeypatch, cli_mod.publish_mod, "run_publish")

    rc = main([
        "publish", "--sessions-root", str(root),
        "other-session", "--session", session.session_id,  # different values
    ])

    assert rc == 1
    assert seen == []
    assert "once" in capsys.readouterr().err


def test_edit_overwrite_guard_errors_with_guidance(tmp_path, capsys):
    rc = main(["edit", "--sessions-root", str(tmp_path / "sessions"), "--overwrite"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "--overwrite-auto" in err  # points at what the user probably meant
    assert "transcribe" in err and "label" in err  # and the real vocabulary


def test_every_subcommand_help_parses():
    # Regression pass: each subcommand still parses its documented arguments
    # well enough to render --help (SystemExit 0) - no alias/guard broke a parser.
    parser = cli_mod.build_parser()
    subs = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    for name in subs.choices:
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([name, "--help"])
        assert exc.value.code == 0, name
