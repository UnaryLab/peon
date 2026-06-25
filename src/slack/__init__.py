"""Slack-facing layer for peon, split out of the former monolithic src/app.py.

The Bolt/Socket-Mode app build, the mention/message handlers, the per-thread
control phrases, the write-mode consent flow, the in-process cron scheduler, the
inbound/outbound file plumbing, and the usage footer all live here as focused
submodules. `src/app.py` is now a thin FACADE that imports from this package and
re-exports the public surface (so the test suite's `app.<name>` references and
its `monkeypatch.setattr(app, ...)` patch targets keep resolving unchanged).
"""
