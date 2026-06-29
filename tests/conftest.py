"""pytest bootstrap: put the PROJECT ROOT on sys.path.

The project root is the parent of this tests/ directory, computed from this
file's location (no hardcoded absolute path). Adding it to sys.path lets the
tests do `from src import ...` without an install step (no setup.py / pyproject).

Hermeticity for model/effort: agents.json is the SINGLE source of truth for an
agent's model and effort (agents.resolve reads ONLY the agent dict's field, with
one code-level fallback). There is no global env-var layer, so a developer's
shell cannot leak into the default-path argv assertions, and no env scrubbing is
needed here.

Hermeticity for STREAM_OUTPUT: streaming defaults ON at run time, but the runner
unit tests that mock subprocess.run assert the LEGACY (non-stream) single-blob
path. An autouse fixture pins STREAM_OUTPUT="0" by default so those tests are
deterministic regardless of the developer's shell; the streaming tests opt back
in with monkeypatch.setenv("STREAM_OUTPUT", "1").

Hermeticity for SHOW_USAGE: the usage footer now defaults ON at run time, but the
general suite asserts footer-free reply text. The same autouse fixture pins
SHOW_USAGE="0" by default so those assertions hold regardless of the developer's
shell; the dedicated telemetry tests set SHOW_USAGE explicitly (or delete it via
monkeypatch.delenv) to exercise the on/off/unset behavior.
"""

import os
import sys

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


@pytest.fixture(autouse=True)
def _default_stream_output_off(monkeypatch):
    """Pin STREAM_OUTPUT="0" (legacy non-stream path) and SHOW_USAGE="0" (footer
    off) for every test unless the test overrides them. STREAM_OUTPUT keeps the
    runner unit tests (which mock subprocess.run and assert the single-blob path +
    original argv) deterministic and shell-immune; SHOW_USAGE keeps the footer-free
    reply assertions stable now that the footer defaults ON. Tests opt back in with
    monkeypatch.setenv(...) (or delenv SHOW_USAGE to assert the unset default).
    """
    monkeypatch.setenv("STREAM_OUTPUT", "0")
    monkeypatch.setenv("SHOW_USAGE", "0")
