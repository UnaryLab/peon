"""Shared test support for the tests/test_*.py suite.

Constants, fake classes, and factories referenced by more than one themed
test module. Single-file fakes stay in their own test file. conftest.py owns
the sys.path bootstrap and the autouse STREAM_OUTPUT/SHOW_USAGE env fixture.
"""

import io
import json
import os
from unittest import mock

# Project root (parent of tests/), for the few tests that spawn the CLI as a
# subprocess; conftest.py also puts this on sys.path for imports.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from src import app as _appmod  # noqa: E402 - optional (needs slack_bolt)

    _HAVE_APP = True
except Exception:  # noqa: BLE001 - slack_bolt absent: skip footer-render asserts
    _appmod = None
    _HAVE_APP = False


# --- Agent dicts + shared string constants (argv/default-path tests) ---
# Agent dicts used in the argv tests. Plain dicts (no model/effort field) exercise
# the default path (the code-level fallback); per-agent tests pass a dict WITH a
# "model"/"effort" field to prove the agents.json field is the sole source.
BRUNEL = {"name": "brunel", "claude_agent": "unarylab-research:project_manager"}
ARISTOTLE = {"name": "aristotle", "claude_agent": "unarylab-research:research_manager"}
CICERO = {"name": "cicero", "claude_agent": None}

SID = "11111111-2222-3333-4444-555555555555"
PROMPT = "what is 2+2?"

# The shipped claude agents pin this model in agents.json; the default-path tests
# use agent dicts with no "model" field, so build_command falls back to this same
# pin. Either way every claude argv carries --model <MODEL> just before the prompt.
MODEL = "claude-opus-4-8[1m]"

# agents.json is now the SINGLE source of truth for model/effort: agents.resolve
# reads ONLY the agent dict's field (with one code-level fallback), with NO env-var
# layer. So these vars no longer affect resolution; we still scrub them defensively
# so a stray export in the developer's shell can never matter, and the default-path
# argv (built from agent dicts WITHOUT a model/effort field) is hermetic.
_LEGACY_MODEL_EFFORT_ENV_VARS = [
    "CLAUDE_MODEL",
    "CLAUDE_EFFORT",
    "CODEX_MODEL",
    "CODEX_EFFORT",
]


def _clear_model_effort_env(monkeypatch):
    """Defensively delenv the legacy model/effort env vars.

    These are no longer read by agents.resolve (agents.json is the source of
    truth), so this is belt-and-suspenders; default-path tests pass agent dicts
    with no model/effort field, exercising the code-level fallback.
    """
    for var in _LEGACY_MODEL_EFFORT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# --- codex agent dict + ids ---
DIJKSTRA = {"name": "dijkstra", "claude_agent": None, "backend": "codex"}
THREAD_ID = "0199abcd-ef01-7234-89ab-cdef01234567"


# --- full aristotle agent dict (name + display_name + backend + claude_agent),
# as the handler/worker/files/quotes/interrupt tests consume it. _HANDLE_AGENT is
# the same entry under the name those tests import; it's a copy (not an alias) so a
# future mutation of one can't leak into the other. ---
_FILE_AGENT = {
    "name": "aristotle",
    "display_name": "Aristotle",
    "backend": "claude",
    "claude_agent": "unarylab-research:research_manager",
}

_HANDLE_AGENT = dict(_FILE_AGENT)


# --- subprocess / process fakes ---
def _fake_proc(returncode=0, stdout="", stderr=""):
    proc = mock.Mock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def _codex_proc_writing(reply, stdout="", returncode=0, stderr=""):
    """Return a fake subprocess.run that writes `reply` to the -o file (the path
    is argv[argv.index("-o") + 1]) and returns a proc with the given stdout, so
    run_codex's read-from-file + parse-stdout paths are both exercised hermetically.
    """

    def _run(argv, **kwargs):
        out_path = argv[argv.index("-o") + 1]
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(reply)
        proc = mock.Mock()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = stderr
        return proc

    return _run


class _FakePopen:
    """Hermetic stand-in for subprocess.Popen for the streaming runner paths.

    `stdout_lines` is the JSONL the CLI would emit (one event per element, no
    trailing newlines needed). The instance is iterable-by-line via .stdout, has a
    readable .stderr, and reports the given returncode after the stream is drained.
    No process, no threads, no network. If `out_file_writer` is given it is called
    with the argv on construction (so a codex fake can write the -o file), mirroring
    how the real codex writes its last-message file during the run.
    """

    def __init__(self, stdout_lines, returncode=0, stderr="", out_file_writer=None):
        self._lines = list(stdout_lines)
        self.returncode = returncode
        self._stderr_text = stderr
        self.stdout = iter(line + "\n" for line in self._lines)
        self.stderr = io.StringIO(stderr)
        self._waited = False
        self._out_file_writer = out_file_writer

    def wait(self, timeout=None):
        self._waited = True
        return self.returncode

    def poll(self):
        # Mirror real Popen: None while running; returncode once wait() has run
        # (the runner calls wait() after draining stdout).
        return self.returncode if self._waited else None

    def kill(self):
        pass


def _fake_popen_factory(stdout_lines, returncode=0, stderr="", writes_to_o=None):
    """Build a subprocess.Popen replacement returning a _FakePopen.

    `writes_to_o`, when set, is the reply text written to the argv's -o file (used
    by the codex streaming tests, whose authoritative reply still comes from -o).
    """

    def _factory(argv, **kwargs):
        if writes_to_o is not None and "-o" in argv:
            out_path = argv[argv.index("-o") + 1]
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(writes_to_o)
        return _FakePopen(stdout_lines, returncode=returncode, stderr=stderr)

    return _factory


def _claude_stream_lines(text_chunks, *, session_id=SID, result=None, **result_extra):
    """JSONL events a streaming claude run emits: a system init, the text deltas,
    then the terminal `result` event (same shape as the non-stream JSON blob).
    `result` defaults to the concatenation of the chunks (the real CLI's final
    text == the streamed text). `result_extra` injects usage/cost/etc fields.
    """
    if result is None:
        result = "".join(text_chunks)
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": session_id})
    ]
    for chunk in text_chunks:
        lines.append(
            json.dumps(
                {
                    "type": "stream_event",
                    "session_id": session_id,
                    "event": {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {"type": "text_delta", "text": chunk},
                    },
                }
            )
        )
    result_event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": session_id,
        "result": result,
    }
    result_event.update(result_extra)
    lines.append(json.dumps(result_event))
    return lines


# --- Slack say/client fakes ---
class _FakeSay:
    """A capturing stand-in for Slack's `say`: records the posted text/thread_ts.

    A callable object (not a function with an attached attribute) so the `.posts`
    list is a real, type-visible member.
    """

    def __init__(self):
        self.posts = []

    def __call__(self, text=None, thread_ts=None):
        self.posts.append({"text": text, "thread_ts": thread_ts})
        return {"ts": "placeholder-ts"}


# The full aristotle entry plus a model/effort the !model/!effort/!reset tests read.
_CONTROL_AGENT = {**_FILE_AGENT, "model": "claude-opus-4-8[1m]", "effort": "xhigh"}


class _FakeClient:
    """Capturing stand-in for Slack's client: records every chat_update call."""

    def __init__(self):
        self.updates = []

    def chat_update(self, channel=None, ts=None, text=None):
        self.updates.append({"channel": channel, "ts": ts, "text": text})
        return {"ok": True}
