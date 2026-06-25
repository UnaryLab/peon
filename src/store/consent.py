"""Write-mode consent (a time-boxed grant on top of the write flag).

Turning write-mode ON is gated by an Approve/Deny consent (app.py posts the
buttons). An approval grants write-mode for a bounded TTL: we store the write
flag together with an absolute expiry epoch ("write_expires_at"). Read time
(is_write_active) compares the expiry against an INJECTABLE clock, so the thread
reverts to read-only automatically once the grant lapses and the tests stay
hermetic (no sleep, no real wall-clock). The field is purely additive: a
write=True entry with no expiry is treated as active (no TTL), so the older
set_override("write", True) path is unaffected.
"""

from __future__ import annotations

from .base import (
    _SESSIONS_LOCK,
    _load_dict_store,
    _now,
    _resolve_path,
    _save_dict_store,
    _session_key,
)
from .overrides import _overrides_path, get_override


def grant_write_consent(agent_name, thread_ts, ttl_minutes, now=None, path=None):
    """Enable write-mode for this thread for `ttl_minutes`, recording the expiry.

    Stores {"write": True, "write_expires_at": now() + ttl_minutes*60} merged into
    the thread's override entry (so a previously-set model/effort is preserved).
    `now` is an injectable zero-arg clock (defaults to the module `_now`) so the
    expiry is deterministic in tests. After the expiry, is_write_active returns
    False and the thread is read-only again.
    """
    if now is None:
        now = _now
    if path is None:
        path = _resolve_path("_overrides_path", _overrides_path)
    composite = _session_key(agent_name, thread_ts)
    expires_at = now() + ttl_minutes * 60
    with _SESSIONS_LOCK:
        overrides = _load_dict_store(path)
        entry = overrides.get(composite) or {}
        entry["write"] = True
        entry["write_expires_at"] = expires_at
        overrides[composite] = entry
        _save_dict_store(overrides, path)


def is_write_active(agent_name, thread_ts, now=None, path=None):
    """Whether write-mode is CURRENTLY in effect for this thread (TTL-aware).

    True iff the thread's override has a truthy "write" AND the grant has not
    expired: either there is no "write_expires_at" (a TTL-less write=True, treated
    as active) or now() is strictly before it. `now` is an injectable zero-arg
    clock (defaults to `_now`) for hermetic tests. This is the single read-time
    gate the worker uses to decide whether to confine the run to the workdir, so an
    expired grant reverts to read-only automatically with no separate sweep.
    """
    if now is None:
        now = _now
    entry = get_override(agent_name, thread_ts, path=path)
    if not entry or not entry.get("write"):
        return False
    expires_at = entry.get("write_expires_at")
    if expires_at is None:
        return True
    return now() < expires_at


def write_expiry(agent_name, thread_ts, path=None):
    """The stored write-mode expiry epoch for this thread, or None.

    None means either no write grant or a TTL-less one. Used by `!write status` to
    report when an active grant lapses.
    """
    entry = get_override(agent_name, thread_ts, path=path)
    if not entry:
        return None
    return entry.get("write_expires_at")
