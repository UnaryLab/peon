from src.slack.handlers import _format_final_response


def test_format_final_response_adds_trailing_blank_line():
    assert _format_final_response("done") == "done\n\n"


def test_format_final_response_does_not_accumulate_trailing_blank_lines():
    assert _format_final_response("done\n\n\n") == "done\n\n"
