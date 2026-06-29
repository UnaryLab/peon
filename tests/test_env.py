"""Authoritative .env loading (src/env.py)."""

import os
import sys

from src.runners import claude_runner

from tests.helpers import _PROJECT_ROOT


# ---------------------------------------------------------------------------
# Authoritative .env loading (src/env.py): .env overrides the shell, including
# SESSIONS_PATH which the claude runner resolves at import time / store access.
# ---------------------------------------------------------------------------


def test_load_env_dotenv_beats_shell(monkeypatch, tmp_path):
    """.env wins over a shell-exported var: load_env(..., override=True) makes a
    CLAUDE_TIMEOUT_MIN in the .env file replace one already exported in the
    shell. (Uses a still-live env var; model/effort no longer come from env.)"""
    from src.env import load_env

    # Simulate a shell-exported value that should LOSE to .env.
    monkeypatch.setenv("CLAUDE_TIMEOUT_MIN", "111")
    env_file = tmp_path / ".env"
    env_file.write_text("CLAUDE_TIMEOUT_MIN=222\n", encoding="utf-8")

    assert load_env(env_file) is True  # dotenv installed -> attempted
    assert os.environ["CLAUDE_TIMEOUT_MIN"] == "222"  # .env beat the shell


def test_load_env_missing_file_is_noop(monkeypatch, tmp_path):
    """A missing .env is a silent no-op (no crash, no clobber of existing vars)."""
    from src.env import load_env

    monkeypatch.setenv("CLAUDE_TIMEOUT_MIN", "111")
    missing = tmp_path / "does-not-exist.env"
    assert not missing.exists()

    load_env(missing)  # must not raise
    assert os.environ["CLAUDE_TIMEOUT_MIN"] == "111"  # untouched


def test_sessions_path_from_env_redirects_store(monkeypatch, tmp_path):
    """A SESSIONS_PATH in os.environ redirects the runner's store, resolved LIVE.

    The claude runner reads SESSIONS_PATH lazily at store-access time, so a value
    placed into os.environ (as load_env would do from .env) redirects where
    sessions.json is read/written, even though the runner was imported earlier."""
    store = tmp_path / "custom-sessions.json"
    monkeypatch.setenv("SESSIONS_PATH", str(store))

    # The resolver honors the env var...
    assert claude_runner._sessions_path() == str(store)

    # ...and a real round-trip through the default-path API writes THERE.
    sid, is_new = claude_runner.get_or_create_session("aristotle", "1.23")
    assert is_new
    assert store.exists()  # store materialized at the env-pointed path
    again, is_new2 = claude_runner.get_or_create_session("aristotle", "1.23")
    assert again == sid and not is_new2


def test_dotenv_sessions_path_wins_over_shell_via_main_import_order(tmp_path):
    """End-to-end proof in the real `python -m src` import order (subprocess).

    A shell-exported SESSIONS_PATH is set in the child's environment; a temp .env
    sets a DIFFERENT SESSIONS_PATH. __main__ calls load_env(override=True) FIRST,
    before .app (hence claude_runner) is imported, so the store resolves to the
    .env path, not the shell one. We run a tiny driver as `python -m src`-style
    code: it imports src.env + the runner exactly as the package does."""
    import subprocess

    shell_store = tmp_path / "shell-sessions.json"
    env_file = tmp_path / ".env"
    dotenv_store = tmp_path / "dotenv-sessions.json"
    env_file.write_text(f"SESSIONS_PATH={dotenv_store}\n", encoding="utf-8")

    # Driver mirrors __main__'s order: load_env(.env, override=True) BEFORE the
    # runner is imported, then read the resolved store path + do a round-trip.
    driver = (
        "from src.env import load_env\n"
        f"load_env(r'{env_file}')\n"  # authoritative, override=True default
        "from src.runners import claude_runner\n"
        "sid, new = claude_runner.get_or_create_session('aristotle', 't')\n"
        "print(claude_runner._sessions_path())\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = _PROJECT_ROOT
    env["SESSIONS_PATH"] = str(shell_store)  # the shell value that must LOSE
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    resolved = proc.stdout.strip().splitlines()[-1]
    assert resolved == str(dotenv_store), proc.stdout + proc.stderr  # .env won
    assert dotenv_store.exists()  # round-trip wrote to the .env path
    assert not shell_store.exists()  # the shell path was NOT used
