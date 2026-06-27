from agent.conversation_loop import _retry_after_seconds_from_error_text


def test_retry_after_seconds_from_error_text_parses_quota_reset_seconds():
    assert _retry_after_seconds_from_error_text("Your quota will reset after 50s") == 51.0


def test_retry_after_seconds_from_error_text_parses_minutes_and_caps():
    assert _retry_after_seconds_from_error_text("rate limit retry after 2 minutes") == 121.0
    assert _retry_after_seconds_from_error_text("quota reset after 10 minutes") == 300.0


def test_retry_after_seconds_from_error_text_ignores_unrelated_text():
    assert _retry_after_seconds_from_error_text("temporary provider failure") is None
