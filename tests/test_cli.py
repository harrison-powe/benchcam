"""End-to-end tests for the CLI flow (new -> run -> mark -> end)."""

from __future__ import annotations

import argparse

from benchcam import cli as cli_mod
from benchcam import dashboard as dashboard_mod
from benchcam import session as session_mod
from benchcam.cli import cmd_fetch, main
from benchcam.markers import read_markers


def test_full_cli_flow(tmp_path, capsys):
    root = str(tmp_path / "sessions")

    assert main(["new", "--sessions-root", root, "--profile", "demo"]) == 0
    assert main(["run", "--sessions-root", root]) == 0
    assert main(["mark", "--sessions-root", root, "power on"]) == 0
    assert main(["mark", "--sessions-root", root, "fault", "--source", "external"]) == 0
    assert main(["end", "--sessions-root", root]) == 0

    session = session_mod.get_active_session(tmp_path / "sessions")
    assert session.status == session_mod.STATUS_ENDED

    rows = read_markers(session.markers_file)
    assert [r["label"] for r in rows] == ["power on", "fault"]
    assert rows[1]["source"] == "external"


def test_mark_without_session_returns_error(tmp_path, capsys):
    root = str(tmp_path / "sessions")
    rc = main(["mark", "--sessions-root", root, "oops"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No active session" in err


def test_sessions_root_env_var_is_default_when_flag_omitted(tmp_path, monkeypatch):
    # Setting BENCHCAM_SESSIONS_ROOT (e.g. to an external SSD path) makes new
    # sessions land there without passing --sessions-root.
    root = tmp_path / "ssd" / "benchcam-sessions"
    monkeypatch.setenv("BENCHCAM_SESSIONS_ROOT", str(root))

    assert main(["new", "--profile", "ssd-test"]) == 0

    active = session_mod.get_active_session(root)
    assert active.profile == "ssd-test"
    assert str(root) in active.storage_path


def test_run_with_obs_reports_clear_error_without_extra(tmp_path, capsys):
    # Without the optional obsws-python extra installed (or without OBS
    # running), selecting the obs recorder must fail cleanly with an actionable
    # message rather than silently falling back to null.
    root = str(tmp_path / "sessions")
    assert main(["new", "--sessions-root", root, "--recorder", "obs"]) == 0
    rc = main(["run", "--sessions-root", root])
    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "obs" in err


def test_dashboard_lan_flag_binds_to_all_interfaces(tmp_path, monkeypatch):
    # --lan must expose on the LAN (0.0.0.0); default stays localhost-only.
    captured = {}
    monkeypatch.setattr(
        dashboard_mod, "serve",
        lambda **kw: captured.update(kw) or 0,
    )
    rc = main(["dashboard", "--sessions-root", str(tmp_path), "--lan", "--no-browser"])
    assert rc == 0
    assert captured["host"] == "0.0.0.0"
    assert captured["open_browser"] is False


def test_dashboard_defaults_to_localhost(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.delenv(dashboard_mod.ENV_DASHBOARD_HOST, raising=False)
    monkeypatch.setattr(dashboard_mod, "serve", lambda **kw: captured.update(kw) or 0)
    rc = main(["dashboard", "--sessions-root", str(tmp_path), "--no-browser"])
    assert rc == 0
    assert captured["host"] == "127.0.0.1"


def test_dashboard_host_env_var_sets_bind_host(tmp_path, monkeypatch):
    monkeypatch.setenv(dashboard_mod.ENV_DASHBOARD_HOST, "0.0.0.0")
    captured = {}
    monkeypatch.setattr(dashboard_mod, "serve", lambda **kw: captured.update(kw) or 0)
    rc = main(["dashboard", "--sessions-root", str(tmp_path), "--no-browser"])
    assert rc == 0
    assert captured["host"] == "0.0.0.0"


def test_fetch_invokes_scp_with_remote_and_dest(tmp_path, monkeypatch):
    # fetch must scp the right "host:remote-root/<session>" to the local
    # sessions root, without performing a real copy or opening anything.
    session_id = "2026-06-23_20-17-17"
    root = tmp_path / "sessions"
    recorded = {}

    def fake_run(argv, *args, **kwargs):
        recorded["argv"] = argv
        return argparse.Namespace(returncode=0)

    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(cli_mod.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: None)
    monkeypatch.setattr(cli_mod.os, "startfile", lambda *a, **k: None, raising=False)

    args = argparse.Namespace(
        sessions_root=str(root),
        session=session_id,
        host="harrison@tatooine.local",
        remote_root="/home/harrison/benchcam/sessions",
        no_open=False,
    )
    rc = cmd_fetch(args)
    assert rc == 0

    argv = recorded["argv"]
    assert argv[0] == "scp"
    assert (
        f"harrison@tatooine.local:/home/harrison/benchcam/sessions/{session_id}"
        in argv
    )
    assert str(root) in argv
