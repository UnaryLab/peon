"""Authoritative `.env` loading, factored out so it is unit-testable.

`.env` is AUTHORITATIVE: it overrides shell-exported environment variables.
This is the single seam both entrypoints use:

  - src/__main__.py calls load_env() as its very first executable code, before
    any module that reads env at import time (notably runners.claude_runner,
    which resolves the session-store path from os.environ). Loading here, first
    and with override=True, makes a SESSIONS_PATH (or the *_TIMEOUT_MIN
    timeouts, ...) set in .env beat both the shell and the import-time defaults.

  - src/app.main() calls it too, so running app directly is self-sufficient.
    The reload is idempotent (override=True re-applies the same values).

python-dotenv is treated as OPTIONAL: if it is not installed, this is a no-op,
so the offline `python -m src manifest <name>` subcommand still works with no
dependencies. A missing `.env` file is likewise a no-op (load_dotenv returns
without raising), not an error.
"""

from pathlib import Path

# Project root = the directory that contains this `src/` package, i.e. one level
# up from this file's parent. Computed from __file__ so it is cwd-independent.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The canonical .env location. Exposed so callers don't recompute the path.
ENV_PATH = PROJECT_ROOT / ".env"


def load_env(path=ENV_PATH, override=True):
    """Load `.env` into os.environ authoritatively (override=True by default).

    Returns True if a load was attempted (dotenv available), False if dotenv is
    not installed (the offline manifest path tolerates this). `override=True`
    makes the file win over shell-exported vars; a missing file is a silent
    no-op. There is intentionally NO override=False path anywhere in the app.
    """
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return False  # dotenv optional; offline `manifest` subcommand still works
    load_dotenv(path, override=override)
    return True
