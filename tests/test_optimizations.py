import asyncio
import os
import tempfile
import sqlite3
from unittest.mock import patch, AsyncMock

from bot import (
    ffmpeg_video_args,
    parse_timestamp,
    _inflight_urls,
    _init_persistent_cache,
    _persist_cache_entry,
    _reddit_shortlink_cache,
    _reddit_has_video_cache,
    _cache_get,
    CACHE_DB_PATH,
    ENCODE_SEMAPHORE,
    JOB_SEMAPHORE,
    MAX_CONCURRENT_JOBS,
    NVENC_MAX_SESSIONS,
    YT_DLP_FRAGMENTS,
    PROCESS_NICE,
    FFMPEG_TIMEOUT,
    GIF_MAX_DURATION,
    BOOST_TIER_LIMITS_MB,
)


def test_concurrent_jobs_at_least_old_default():
    assert MAX_CONCURRENT_JOBS >= 3


def test_fragments_at_least_old_default():
    assert YT_DLP_FRAGMENTS >= 4


def test_encode_semaphore_exists():
    assert ENCODE_SEMAPHORE._value == NVENC_MAX_SESSIONS


def test_ffmpeg_args_h264_nvenc_p5():
    args = ffmpeg_video_args(use_nvenc=True)
    assert "-c:v" in args
    assert "h264_nvenc" in args
    assert "p5" in args
    assert "hq" in args


def test_ffmpeg_args_hevc_nvenc():
    args = ffmpeg_video_args(use_nvenc=True, use_hevc=True)
    assert "hevc_nvenc" in args
    assert "p5" in args


def test_ffmpeg_args_libx264_fallback():
    args = ffmpeg_video_args(use_nvenc=False)
    assert "libx264" in args
    assert "veryfast" in args


def test_ffmpeg_args_libx265_software():
    args = ffmpeg_video_args(use_nvenc=False, use_hevc=True)
    assert "libx265" in args
    assert "fast" in args


def test_inflight_url_dedup():
    _inflight_urls.clear()
    _inflight_urls.add("https://example.com/video")
    assert "https://example.com/video" in _inflight_urls
    _inflight_urls.discard("https://example.com/video")
    assert "https://example.com/video" not in _inflight_urls


def test_persistent_cache_roundtrip():
    db_path = os.path.join(tempfile.gettempdir(), "test_cove_cache.db")
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS url_cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            cache_type TEXT NOT NULL,
            expires_at REAL NOT NULL
        )""")
        conn.execute(
            "INSERT OR REPLACE INTO url_cache VALUES (?, ?, ?, ?)",
            ("test_key", "test_value", "shortlink", 9999999999.0),
        )
        conn.commit()

        row = conn.execute("SELECT value FROM url_cache WHERE key = ?", ("test_key",)).fetchone()
        assert row is not None
        assert row[0] == "test_value"

        conn.execute("DELETE FROM url_cache WHERE key = ?", ("test_key",))
        conn.commit()
        conn.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_persist_cache_entry_bool():
    _persist_cache_entry("test_bool_key", True, "has_video", 3600)
    try:
        conn = sqlite3.connect(CACHE_DB_PATH)
        row = conn.execute(
            "SELECT value, cache_type FROM url_cache WHERE key = ?",
            ("test_bool_key",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "1"
        assert row[1] == "has_video"
    finally:
        conn = sqlite3.connect(CACHE_DB_PATH)
        conn.execute("DELETE FROM url_cache WHERE key = ?", ("test_bool_key",))
        conn.commit()
        conn.close()


def test_persist_cache_entry_string():
    _persist_cache_entry("test_str_key", "https://reddit.com/r/test/comments/abc", "shortlink", 3600)
    try:
        conn = sqlite3.connect(CACHE_DB_PATH)
        row = conn.execute(
            "SELECT value, cache_type FROM url_cache WHERE key = ?",
            ("test_str_key",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "https://reddit.com/r/test/comments/abc"
        assert row[1] == "shortlink"
    finally:
        conn = sqlite3.connect(CACHE_DB_PATH)
        conn.execute("DELETE FROM url_cache WHERE key = ?", ("test_str_key",))
        conn.commit()
        conn.close()


def test_process_nice_default():
    assert PROCESS_NICE == 10


def test_ffmpeg_timeout_default():
    assert FFMPEG_TIMEOUT == 300


def test_parse_timestamp_seconds():
    assert parse_timestamp("90") == 90.0


def test_parse_timestamp_minutes_seconds():
    assert parse_timestamp("1:30") == 90.0


def test_parse_timestamp_hours_minutes_seconds():
    assert parse_timestamp("1:30:00") == 5400.0


def test_parse_timestamp_zero():
    assert parse_timestamp("0") == 0.0
    assert parse_timestamp("0:00") == 0.0


def test_parse_timestamp_decimal():
    assert parse_timestamp("1:30.5") == 90.5


def test_parse_timestamp_invalid():
    assert parse_timestamp("abc") is None
    assert parse_timestamp("") is None
    assert parse_timestamp("::") is None


def test_parse_timestamp_negative_clamps_to_zero():
    assert parse_timestamp("-5") == 0.0


def test_gif_max_duration():
    assert GIF_MAX_DURATION == 10.0


def test_boost_tier_limits_updated():
    assert BOOST_TIER_LIMITS_MB[0] >= 24.0
    assert BOOST_TIER_LIMITS_MB[1] >= 24.0
