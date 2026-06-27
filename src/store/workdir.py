"""Per-thread workdir (shared by both backends + uploads).

Each thread gets a dedicated directory under WORKDIR_BASE (env, default
~/Projects/.peon-workdirs), namespaced by agent + thread_ts, used as the run's cwd
and scanned for files to upload back. This is the single owner of that path
scheme; app.py (for outbound file uploads) and both runners (cwd) reuse it. The
base is read LIVE from os.environ so a SIGHUP .env reload takes effect. The
default base is ABSOLUTE (~/Projects/.peon-workdirs, OUTSIDE this repo) so a run's
default cwd is never the framework source; get_workdir always returns an ABSOLUTE
path (the subprocess cwd needs one). Set an explicit WORKDIR_BASE to override.
"""

from __future__ import annotations

import os
import re

_DEFAULT_WORKDIR_BASE = "~/Projects/.peon-workdirs"


def _workdir_base():
    """Resolve the workdir base dir, reading WORKDIR_BASE from os.environ LIVE.

    Falls back to ~/Projects/.peon-workdirs (absolute, OUTSIDE the repo) when
    unset/empty. Read lazily (per call), not an import-time constant, so a
    WORKDIR_BASE loaded from .env takes effect even though .env is loaded after
    this module is imported. expanduser is applied so the default ~ and an
    explicit ~/... override both resolve to the home dir.
    """
    return os.path.expanduser(os.environ.get("WORKDIR_BASE") or _DEFAULT_WORKDIR_BASE)


def get_workdir(agent_name, thread_ts, create=False):
    """The per-thread workdir path for (agent_name, thread_ts).

    Namespaced by agent + thread_ts under WORKDIR_BASE, with both components
    sanitized to a filesystem-safe token (the thread_ts carries a dot, which is
    fine, but Slack ids are otherwise opaque). Pure path by default; `create=True`
    makes the directory on demand (the worker uses it before each run).

    ALWAYS returns an ABSOLUTE path: the joined path is run through
    os.path.abspath (a no-op on the absolute default, but it also resolves a
    relative WORKDIR_BASE override against the process cwd). An absolute path is
    required because the subprocess cwd cannot take a relative/ambiguous path. With
    the default base this resolves to
    <home>/Projects/.peon-workdirs/<agent>/<thread>, i.e. OUTSIDE the repo.

    Shared helper: both runners use it (subprocess cwd) and app.py reuses it for
    the outbound file-upload scan.
    """
    safe_agent = _safe_token(agent_name)
    safe_thread = _safe_token(thread_ts)
    path = os.path.abspath(os.path.join(_workdir_base(), safe_agent, safe_thread))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _safe_token(value):
    """Sanitize a path component: keep [A-Za-z0-9._-], replace the rest with '_'.

    A token that is empty or all dots ('', '.', '..') survives the regex but would
    let get_workdir escape WORKDIR_BASE (e.g. a "../.." path), so it is collapsed
    to a safe placeholder. A real thread_ts carries digits and a real agent name
    carries letters, so neither is pure-dots and both stay byte-identical.
    """
    out = re.sub(r"[^A-Za-z0-9._-]", "_", str(value))
    if not out or set(out) <= {"."}:
        out = "_"
    return out
