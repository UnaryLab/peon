"""Session store (JSON file, keyed on "<agent_name>:<thread_ts>").

Backend-agnostic store access used by the unified session seam (see runners.py
and app.py). The key is (agent_name, thread_ts) for EVERY backend, so two
agents in one Slack thread, or one agent across two threads, stay independent.
This is the single owner of the JSON store + lock; the codex path does not
touch the store, app.py persists whatever the runner returns via set_session.
"""

from __future__ import annotations

import uuid

from .base import (
    _SESSIONS_LOCK,
    _load_dict_store,
    _resolve_path,
    _save_dict_store,
    _session_key,
    _sessions_path,
)


def get_or_create_session(agent_name, thread_ts, path=None):
    """Resolve the claude session id for (agent_name, thread_ts).

    Returns (session_id, is_new):
      - first time for this key: mint a fresh uuid4, persist it, is_new=True
      - subsequent times:        return the stored id, is_new=False

    A new Slack thread = a new key = a fresh context. Different agent OR
    different thread => different key => different session => independent context.
    """
    if path is None:
        path = _resolve_path("_sessions_path", _sessions_path)
    key = _session_key(agent_name, thread_ts)
    # Lock the whole read-modify-write: background worker threads can first-touch
    # the same new key concurrently, and an unlocked path would mint two uuids and
    # clobber one. The lock is in-process only (see _SESSIONS_LOCK note above).
    with _SESSIONS_LOCK:
        sessions = _load_dict_store(path)
        existing = sessions.get(key)
        if existing:
            return existing, False
        new_id = str(uuid.uuid4())
        sessions[key] = new_id
        _save_dict_store(sessions, path)
        return new_id, True


def get_session(agent_name, thread_ts, path=None):
    """Return the stored session id for (agent_name, thread_ts), or None.

    None means "no prior session for this key" -> the runner starts a fresh one.
    Backend-agnostic: claude stores a uuid, codex stores a thread_id; both are
    just opaque strings under the same composite key.
    """
    if path is None:
        path = _resolve_path("_sessions_path", _sessions_path)
    key = _session_key(agent_name, thread_ts)
    with _SESSIONS_LOCK:
        return _load_dict_store(path).get(key)


def set_session(agent_name, thread_ts, session_id, path=None):
    """Persist `session_id` for (agent_name, thread_ts) (read-modify-write)."""
    if path is None:
        path = _resolve_path("_sessions_path", _sessions_path)
    key = _session_key(agent_name, thread_ts)
    with _SESSIONS_LOCK:
        sessions = _load_dict_store(path)
        sessions[key] = session_id
        _save_dict_store(sessions, path)
