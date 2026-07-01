"""Backend dispatcher: map a registry entry's `backend` to its runner module.

A runner is any module exposing the unified seam
  answer(agent, prompt, prior_session_id, timeout=None, overrides=None,
         on_update=None, cancel=None, on_session=None)
      -> (reply_text, session_id_to_store, meta)
where prior_session_id is the stored session id for this (agent, thread) or None
on the first message, session_id_to_store is whatever id the caller should
persist for resumes, and meta is the usage dict {context_pct, tokens, cost_usd,
duration_s} (any field None). This hides the difference between the two backends'
session lifecycles (claude pre-generates a uuid; codex mints its own thread_id
and only reports it after the run) behind one call shape.

Imports nothing from slack_bolt, so app.py and the tests can both use it without
Slack installed. Adding a third backend is a one-line addition to _RUNNERS.
"""

from . import claude_runner, codex_runner

_RUNNERS = {
    "claude": claude_runner,
    "codex": codex_runner,
}


def get_runner(backend):
    """Return the runner module for `backend` ("claude" or "codex").

    Raises ValueError on an unknown backend so a typo in the registry fails loud
    at dispatch rather than silently doing nothing.
    """
    try:
        return _RUNNERS[backend]
    except KeyError:
        known = ", ".join(sorted(_RUNNERS))
        raise ValueError(f"unknown backend {backend!r}; known backends: {known}")
