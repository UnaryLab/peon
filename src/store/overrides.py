"""Override store (JSON file, keyed on "<agent_name>:<thread_ts>").

A per-thread, per-agent model/effort override settable from Slack control
phrases. It lives as a SIBLING of sessions.json in the same directory (derived
from _sessions_path()'s dir), so SESSIONS_PATH is honored with no new env var.
Same composite key, same lock, same JSON-file shape as the session store; the
stored value per key is a dict like {"model": str, "effort": str} (either key
optional). The session store is the pattern this mirrors; the two files are
independent (this one never touches sessions.json).
"""

from __future__ import annotations

from .base import (
    _SESSIONS_LOCK,
    _load_dict_store,
    _resolve_path,
    _save_dict_store,
    _session_key,
    _sibling_store_path,
)


def _overrides_path():
    """Resolve the override-store path: a sibling of the sessions path."""
    return _sibling_store_path("overrides.json")


def get_override(agent_name, thread_ts, path=None):
    """Return the stored override dict for (agent_name, thread_ts), or None.

    The dict carries an optional "model" and/or "effort" string. None means no
    override is set for this key, so the agents.json defaults apply unchanged.
    """
    if path is None:
        path = _resolve_path("_overrides_path", _overrides_path)
    key = _session_key(agent_name, thread_ts)
    with _SESSIONS_LOCK:
        return _load_dict_store(path).get(key)


def set_override(agent_name, thread_ts, key, value, path=None):
    """Merge a single override (`key` is "model" or "effort") for this thread.

    Read-modify-write under the lock: an existing entry is merged (so setting
    just "effort" preserves a previously-set "model"); an absent entry is
    created. Mirrors set_session's persistence.
    """
    if path is None:
        path = _resolve_path("_overrides_path", _overrides_path)
    composite = _session_key(agent_name, thread_ts)
    with _SESSIONS_LOCK:
        overrides = _load_dict_store(path)
        entry = overrides.get(composite) or {}
        entry[key] = value
        overrides[composite] = entry
        _save_dict_store(overrides, path)


def clear_override(agent_name, thread_ts, path=None):
    """Remove this thread's override entry entirely (back to agents.json defaults).

    A no-op if the key is absent. Read-modify-write under the lock.
    """
    if path is None:
        path = _resolve_path("_overrides_path", _overrides_path)
    composite = _session_key(agent_name, thread_ts)
    with _SESSIONS_LOCK:
        overrides = _load_dict_store(path)
        if composite in overrides:
            del overrides[composite]
            _save_dict_store(overrides, path)
