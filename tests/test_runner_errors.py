from src.runners.common import format_process_failure


def test_format_process_failure_uses_stderr():
    message = format_process_failure("claude", 1, stderr="boom\nmore detail")

    assert message == "claude exited with code 1: boom more detail"


def test_format_process_failure_falls_back_to_stdout():
    message = format_process_failure("codex", 1, stdout="context length exceeded")

    assert (
        message
        == "codex exited with code 1: likely token/context limit: context length exceeded"
    )


def test_format_process_failure_handles_missing_output():
    message = format_process_failure("claude", 1)

    assert message == "claude exited with code 1: no stderr/stdout captured"
