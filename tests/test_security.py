from bot import (
    _sanitize_error_line,
    _sanitize_filename,
    _check_user_rate_limit,
    USER_RATE_LIMIT,
    _user_request_times,
)


def test_sanitize_strips_filesystem_paths():
    raw = "ERROR: /home/user/.config/yt-dlp/cookies.txt: file not found"
    result = _sanitize_error_line(raw)
    assert "/home/" not in result
    assert "cookies" not in result.lower()


def test_sanitize_strips_ip_addresses():
    raw = "ERROR: Unable to connect to 192.168.1.50:8080"
    result = _sanitize_error_line(raw)
    assert "192.168" not in result


def test_sanitize_strips_env_variable_references():
    raw = "ERROR: DISCORD_TOKEN=abc123 invalid"
    result = _sanitize_error_line(raw)
    assert "abc123" not in result


def test_sanitize_preserves_clean_error():
    raw = "ERROR: Video unavailable"
    result = _sanitize_error_line(raw)
    assert result == "Video unavailable"


def test_sanitize_fallback_on_empty():
    assert _sanitize_error_line("") == "Download failed."
    assert _sanitize_error_line("   ") == "Download failed."


def test_sanitize_strips_error_prefix():
    raw = "ERROR: Something went wrong"
    result = _sanitize_error_line(raw)
    assert result == "Something went wrong"


def test_filename_strips_path_traversal():
    assert ".." not in _sanitize_filename("../../etc/passwd.mp4")


def test_filename_strips_null_bytes():
    assert "\x00" not in _sanitize_filename("video\x00.mp4")


def test_filename_preserves_normal_name():
    assert _sanitize_filename("My Cool Video.mp4") == "My Cool Video.mp4"


def test_filename_limits_length():
    long_name = "a" * 300 + ".mp4"
    result = _sanitize_filename(long_name)
    assert len(result) <= 200


def test_filename_fallback_on_empty():
    assert _sanitize_filename("") == "video"


def test_rate_limit_allows_first_request():
    _user_request_times.clear()
    assert _check_user_rate_limit(99999) is True


def test_rate_limit_blocks_after_exceeded():
    _user_request_times.clear()
    user_id = 88888
    for _ in range(USER_RATE_LIMIT):
        assert _check_user_rate_limit(user_id) is True
    assert _check_user_rate_limit(user_id) is False
