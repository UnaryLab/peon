"""Placeholder quotes + handlers._handle placeholder text."""

import json
from unittest import mock


from tests.helpers import (
    _FakeSay,
    _HANDLE_AGENT,
)


# ---------------------------------------------------------------------------
# Placeholder quotes (src/slack/quotes.py): random_quote() loads a flat JSON array
# of strings from quotes.json (project-root-anchored, mtime-cached) and returns a
# random member, or "" when the file is missing/empty/invalid. Hermetic: each test
# redirects _QUOTES_PATH to a tmp file and resets the mtime cache so it controls
# the data (never depends on the real repo quotes.json contents or count).
# ---------------------------------------------------------------------------


def _set_quotes_file(monkeypatch, tmp_path, contents):
    """Point quotes._QUOTES_PATH at a tmp file with `contents` (or no file if None),
    reset the mtime cache, and return the quotes module. Keeps the loader hermetic.
    """
    from src.slack import quotes as quotes_mod

    qpath = str(tmp_path / "quotes.json")
    if contents is not None:
        with open(qpath, "w", encoding="utf-8") as f:
            f.write(contents)
    monkeypatch.setattr(quotes_mod, "_QUOTES_PATH", qpath)
    monkeypatch.setattr(quotes_mod, "_cache", None)  # reset mtime cache
    return quotes_mod


def test_random_quote_returns_member_of_list(monkeypatch, tmp_path):
    # With a populated quotes file, random_quote() returns one of its entries.
    entries = ["Work work.", "Ready to work.", "Yes, milord?"]
    quotes_mod = _set_quotes_file(monkeypatch, tmp_path, json.dumps(entries))
    for _ in range(20):  # several draws: every result must be a known member
        assert quotes_mod.random_quote() in entries


def test_random_quote_empty_when_missing_or_invalid(monkeypatch, tmp_path):
    # Missing file, empty array, empty file, non-list, and invalid JSON all yield ""
    # (the loader is graceful, so the caller falls back to the default placeholder).
    # Missing file (contents=None means no file is written).
    quotes_mod = _set_quotes_file(monkeypatch, tmp_path, None)
    assert quotes_mod.random_quote() == ""
    # Empty JSON array.
    quotes_mod = _set_quotes_file(monkeypatch, tmp_path, "[]")
    assert quotes_mod.random_quote() == ""
    # Completely empty file (invalid JSON).
    quotes_mod = _set_quotes_file(monkeypatch, tmp_path, "")
    assert quotes_mod.random_quote() == ""
    # Valid JSON but not a list.
    quotes_mod = _set_quotes_file(monkeypatch, tmp_path, '{"a": 1}')
    assert quotes_mod.random_quote() == ""
    # Malformed JSON.
    quotes_mod = _set_quotes_file(monkeypatch, tmp_path, "not json{")
    assert quotes_mod.random_quote() == ""


# ---------------------------------------------------------------------------
# handlers._handle placeholder text: the posted placeholder IS a random "peon"
# worker quote (random_quote()) when quotes.json has entries; an empty quote
# (no/invalid quotes file) falls back to the default "{display_name} is
# thinking...". Holds on both the @-mention and in-thread message paths. The slow
# worker is stubbed out (threading.Thread -> no-op) so no real CLI/Slack call happens.
# ---------------------------------------------------------------------------


class _NoopThread:
    """A drop-in for threading.Thread whose start() does nothing.

    _handle spawns a background worker after posting the placeholder; stubbing the
    thread keeps the test to just the placeholder logic (hermetic, no CLI run).
    """

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


def _mention_event(text="<@U0BOT> hi", ts="111.001"):
    # A minimal user app_mention event with a UNIQUE ts so seen_before (the in-proc
    # dedup) never collides with another test's event id.
    return {"channel": "C1", "ts": ts, "text": text, "user": "U1"}


def test_format_thread_history_includes_prior_visible_messages_only():
    from src.slack import handlers

    history = handlers._format_thread_history(
        [
            {"ts": "100.000", "user": "U1", "text": "<@UA> do the work"},
            {
                "ts": "100.100",
                "bot_profile": {"name": "Agent A"},
                "text": "result Y",
            },
            {"ts": "100.200", "user": "U2", "text": "<@UB> what did A say?"},
            {"ts": "100.300", "subtype": "message_deleted", "text": "gone"},
        ],
        current_ts="100.200",
    )

    assert "Visible Slack thread so far:" in history
    assert "- U1: <@UA> do the work" in history
    assert "- Agent A: result Y" in history
    assert "what did A say" not in history
    assert "gone" not in history


def test_handle_includes_prior_thread_history_in_prompt(monkeypatch):
    from src.slack import handlers

    captured = {}

    class _CaptureThread:
        def __init__(self, target=None, args=None, daemon=None):
            captured["target"] = target
            captured["args"] = args
            captured["daemon"] = daemon

        def start(self):
            pass

    client = mock.Mock()
    client.conversations_replies.return_value = {
        "messages": [
            {"ts": "910.000", "user": "U1", "text": "<@UA> compute X"},
            {
                "ts": "910.100",
                "bot_profile": {"name": "Agent A"},
                "text": "X is 42",
            },
            {"ts": "910.200", "user": "U2", "text": "<@UB> what did A say?"},
        ]
    }
    monkeypatch.setattr(handlers.threading, "Thread", _CaptureThread)
    monkeypatch.setattr(handlers.quotes, "random_quote", lambda: "")

    event = _mention_event(text="<@U0BOT> what did A say?", ts="910.200")
    event["thread_ts"] = "910.000"
    say = _FakeSay()
    handlers._handle(_HANDLE_AGENT, event, client, say)

    prompt = captured["args"][4]
    assert "Visible Slack thread so far:" in prompt
    assert "Agent A: X is 42" in prompt
    assert "Current request:\nwhat did A say?" in prompt
    assert prompt.count("what did A say?") == 1
    client.conversations_replies.assert_called_once_with(
        channel="C1",
        ts="910.000",
        limit=handlers._THREAD_HISTORY_LIMIT,
    )


def test_handle_placeholder_is_quote(monkeypatch):
    from src.slack import handlers

    monkeypatch.setattr(handlers.threading, "Thread", _NoopThread)
    monkeypatch.setattr(handlers.quotes, "random_quote", lambda: "Work work.")
    say = _FakeSay()
    client = mock.Mock()
    handlers._handle(_HANDLE_AGENT, _mention_event(ts="900.001"), client, say)
    # The placeholder post (the only say call here) uses the quote verbatim.
    assert len(say.posts) == 1
    assert say.posts[0]["text"] == "Work work."


def test_handle_placeholder_falls_back_to_default_on_empty_quote(monkeypatch):
    from src.slack import handlers

    monkeypatch.setattr(handlers.threading, "Thread", _NoopThread)
    default = f"{_HANDLE_AGENT['display_name']} is thinking..."

    # Empty quote ("" = no/invalid quotes file): placeholder is the default text.
    monkeypatch.setattr(handlers.quotes, "random_quote", lambda: "")
    say = _FakeSay()
    handlers._handle(_HANDLE_AGENT, _mention_event(ts="902.001"), mock.Mock(), say)
    assert len(say.posts) == 1
    assert say.posts[0]["text"] == default
