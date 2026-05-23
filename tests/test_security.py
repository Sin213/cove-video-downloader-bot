from bot import _sanitize_error_line


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
