"""app.py file attachments: inbound download + outbound named upload."""

import os

from src.runners import claude_runner

from tests.helpers import (
    _FILE_AGENT,
    _appmod,
    _HAVE_APP,
)


# ---------------------------------------------------------------------------
# app.py file attachments: inbound download (url_private -> local path appended to
# the prompt) and outbound upload (files produced in a designated workdir uploaded
# back into the thread). All HTTP + Slack I/O is mocked: the download seam
# (_http_get_bytes) is patched and a fake client captures files_upload_v2; NO real
# network/Slack call is ever made. src.app imported via the _HAVE_APP guard.
# ---------------------------------------------------------------------------


class _FakeFileClient:
    """Capturing stand-in for Slack's client for the attachment tests.

    Carries a .token (used by the inbound downloader for the Bearer header) and a
    files_upload_v2 that records every call. boom=True makes uploads raise so the
    swallow-error path is exercisable.
    """

    def __init__(self, token="xoxb-test", boom=False):
        self.token = token
        self.uploads = []
        self.boom = boom

    def files_upload_v2(self, channel=None, thread_ts=None, file=None, filename=None):
        if self.boom:
            raise RuntimeError("upload failed")
        self.uploads.append(
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "file": file,
                "filename": filename,
            }
        )
        return {"ok": True}


def test_download_attachments_appends_local_paths_to_prompt(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # Land downloads in tmp_path (no system-temp pollution); mock the HTTP seam.
    monkeypatch.setattr(_appmod, "_attachments_dir", lambda thread_ts: str(tmp_path))
    seen = {}

    def fake_get(url, token):
        seen["url"] = url
        seen["token"] = token
        return b"PNGDATA"

    monkeypatch.setattr(_appmod, "_http_get_bytes", fake_get)
    client = _FakeFileClient(token="xoxb-bot")
    files = [{"name": "diagram.png", "url_private": "https://files.slack.com/a.png"}]

    paths = _appmod._download_attachments(client, files, "T1")
    assert len(paths) == 1
    assert paths[0] == os.path.join(str(tmp_path), "diagram.png")
    # The bot token was used for the Bearer header, and the bytes were written.
    assert seen["token"] == "xoxb-bot"
    assert seen["url"] == "https://files.slack.com/a.png"
    with open(paths[0], "rb") as f:
        assert f.read() == b"PNGDATA"
    # The local path is appended to the prompt.
    prompt = _appmod._append_attachments("look at this", paths)
    assert prompt == "look at this\n\n[Attached files: " + paths[0] + "]"


def test_download_attachments_no_files_is_noop(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # Should never even touch the HTTP seam when there are no files.
    monkeypatch.setattr(
        _appmod,
        "_http_get_bytes",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")),
    )
    client = _FakeFileClient()
    assert _appmod._download_attachments(client, None, "T1") == []
    assert _appmod._download_attachments(client, [], "T1") == []
    # Prompt is byte-identical when there are no attachments.
    assert _appmod._append_attachments("hi", []) == "hi"


def test_download_attachments_skips_failed_download(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    monkeypatch.setattr(_appmod, "_attachments_dir", lambda thread_ts: str(tmp_path))

    def fake_get(url, token):
        if "bad" in url:
            raise OSError("network down")
        return b"GOOD"

    monkeypatch.setattr(_appmod, "_http_get_bytes", fake_get)
    client = _FakeFileClient()
    files = [
        {"name": "bad.png", "url_private": "https://files.slack.com/bad.png"},
        {"name": "good.png", "url_private": "https://files.slack.com/good.png"},
        {"name": "no-url.png"},  # no url_private at all -> skipped silently
    ]
    paths = _appmod._download_attachments(client, files, "T1")
    # Only the good download survives; the failed one and the URL-less one are dropped.
    assert paths == [os.path.join(str(tmp_path), "good.png")]


def test_attachments_dir_is_per_thread_and_created(tmp_path, monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # Redirect the system temp base so we don't pollute the real /tmp.
    monkeypatch.setattr(_appmod.tempfile, "gettempdir", lambda: str(tmp_path))
    d1 = _appmod._attachments_dir("T1")
    d2 = _appmod._attachments_dir("T2")
    assert os.path.isdir(d1) and os.path.isdir(d2)
    assert d1 != d2  # different threads -> different dirs


def test_maybe_upload_named_no_names_skips_upload(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # No marker -> no names -> the outbound path is a no-op; the workdir is never
    # even resolved and files_upload_v2 is NEVER called.
    monkeypatch.setattr(
        claude_runner,
        "get_workdir",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not resolve")),
        raising=False,
    )
    client = _FakeFileClient()
    count = _appmod._maybe_upload_named(client, "C1", "T1", _FILE_AGENT, [])
    assert count == 0
    assert client.uploads == []


def test_maybe_upload_named_no_workdir_skips_upload(monkeypatch):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # Names given but no get_workdir helper -> no workdir -> still a no-op.
    monkeypatch.delattr(claude_runner, "get_workdir", raising=False)
    client = _FakeFileClient()
    count = _appmod._maybe_upload_named(client, "C1", "T1", _FILE_AGENT, ["result.txt"])
    assert count == 0
    assert client.uploads == []


def test_maybe_upload_named_uploads_resolved_files(monkeypatch, tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    workdir = tmp_path / "wd"
    workdir.mkdir()
    produced = workdir / "result.txt"
    produced.write_text("generated output", encoding="utf-8")
    # A sibling the run did NOT name must never be uploaded.
    (workdir / "ignored.txt").write_text("noise", encoding="utf-8")
    monkeypatch.setattr(
        claude_runner, "get_workdir", lambda name, ts: str(workdir), raising=False
    )
    client = _FakeFileClient()
    count = _appmod._maybe_upload_named(client, "C1", "T1", _FILE_AGENT, ["result.txt"])
    assert count == 1
    assert len(client.uploads) == 1
    up = client.uploads[0]
    assert up["channel"] == "C1"
    assert up["thread_ts"] == "T1"
    assert up["file"] == os.path.realpath(str(produced))
    assert up["filename"] == "result.txt"


def test_parse_file_marker_extracts_and_strips():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # No marker -> text unchanged, no names.
    assert _appmod._parse_file_marker("just a normal reply") == (
        "just a normal reply",
        [],
    )
    # Marker -> names parsed (trimmed, empties dropped) and stripped off the reply.
    clean, names = _appmod._parse_file_marker(
        "Here is your plot.\n<<files: plot.png , data.csv,>>"
    )
    assert clean == "Here is your plot."
    assert names == ["plot.png", "data.csv"]
    # Two markers (degenerate; the run is meant to emit one, last): names come
    # from the LAST, and everything from the FIRST marker onward is stripped.
    clean, names = _appmod._parse_file_marker("a <<files: x>> b <<files: y>>")
    assert names == ["y"]
    assert clean == "a"
    # Falsy input is returned as-is.
    assert _appmod._parse_file_marker("") == ("", [])


def test_strip_file_marker_removes_complete_and_partial():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    # A complete marker is removed.
    assert _appmod._strip_file_marker("done.\n<<files: a.png>>") == "done."
    # A partial/unterminated trailing marker (mid-stream) is removed too.
    assert _appmod._strip_file_marker("almost <<files: pl") == "almost"
    # No marker -> unchanged.
    assert _appmod._strip_file_marker("plain text") == "plain text"


def test_resolve_named_files_resolves_and_rejects_escapes(tmp_path):
    if not _HAVE_APP:
        return
    assert _appmod is not None
    rp = os.path.realpath
    workdir = tmp_path / "wd"
    (workdir / "sub").mkdir(parents=True)
    top = workdir / "top.txt"
    top.write_text("top", encoding="utf-8")
    nested = workdir / "sub" / "deep.csv"
    nested.write_text("deep", encoding="utf-8")
    # A secret OUTSIDE the workdir must never be reachable.
    secret = tmp_path / "secret.txt"
    secret.write_text("nope", encoding="utf-8")

    # Resolves by relative path and by bare basename (via the walk).
    assert _appmod._resolve_named_files(str(workdir), ["top.txt"]) == [rp(str(top))]
    assert _appmod._resolve_named_files(str(workdir), ["sub/deep.csv"]) == [
        rp(str(nested))
    ]
    assert _appmod._resolve_named_files(str(workdir), ["deep.csv"]) == [rp(str(nested))]
    # `..` escape, an absolute path outside, and a missing name are all rejected.
    assert _appmod._resolve_named_files(str(workdir), ["../secret.txt"]) == []
    assert _appmod._resolve_named_files(str(workdir), [str(secret)]) == []
    assert _appmod._resolve_named_files(str(workdir), ["ghost.txt"]) == []
    # Falsy / non-dir inputs are empty, never an error.
    assert _appmod._resolve_named_files("", ["x"]) == []
    assert _appmod._resolve_named_files(str(workdir), []) == []
    assert _appmod._resolve_named_files(str(workdir / "nope"), ["x"]) == []


def test_upload_workdir_files_swallows_errors():
    if not _HAVE_APP:
        return
    assert _appmod is not None
    client = _FakeFileClient(boom=True)  # every upload raises
    # Must not raise; returns 0 since nothing landed.
    count = _appmod._upload_workdir_files(client, "C1", "T1", ["/abs/a.png"])
    assert count == 0
