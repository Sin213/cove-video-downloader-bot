import asyncio
import json
import os
import sqlite3
import tempfile
import time
from unittest.mock import MagicMock

import discord

import bot
from bot import (
    canonical_url_for_key,
    ffmpeg_video_args,
    duration_from_media_info,
    parse_timestamp,
    _inflight_urls,
    _inflight_key,
    _cache_write_queue,
    _flush_cache_writes,
    CACHE_DB_PATH,
    ENCODE_SEMAPHORE,
    MAX_CONCURRENT_JOBS,
    NVENC_MAX_SESSIONS,
    YT_DLP_FRAGMENTS,
    PROCESS_NICE,
    FFMPEG_TIMEOUT,
    GIF_MAX_DURATION,
    BOOST_TIER_LIMITS_MB,
    MAX_QUEUED_JOBS,
    PipelineTimer,
    _job_queue_status,
    _release_job_slot,
    _try_reserve_job_slot,
    should_use_aria2c,
    _run_download_phase,
    send_file_with_retry,
    _run_ytdlp_with_info_cache,
    _get_cached_ytdlp_info,
    _set_cached_ytdlp_info,
    _probe_youtube_quality,
    JOB_SEMAPHORE,
)


def test_concurrent_jobs_at_least_old_default():
    assert MAX_CONCURRENT_JOBS >= 3


def test_queued_jobs_default_positive():
    assert MAX_QUEUED_JOBS >= 1


def test_fragments_at_least_old_default():
    assert YT_DLP_FRAGMENTS >= 4


def test_encode_semaphore_exists():
    assert ENCODE_SEMAPHORE._value == NVENC_MAX_SESSIONS


def test_ffmpeg_args_h264_nvenc():
    args = ffmpeg_video_args(use_nvenc=True)
    assert "-c:v" in args
    assert "h264_nvenc" in args
    assert "p2" in args


def test_ffmpeg_args_hevc_nvenc():
    args = ffmpeg_video_args(use_nvenc=True, use_hevc=True)
    assert "h264_nvenc" in args
    assert "p2" in args


def test_ffmpeg_args_libx264_fallback():
    args = ffmpeg_video_args(use_nvenc=False)
    assert "libx264" in args
    assert "veryfast" in args


def test_ffmpeg_args_libx265_software():
    args = ffmpeg_video_args(use_nvenc=False, use_hevc=True)
    assert "libx264" in args
    assert "veryfast" in args


def test_inflight_url_dedup():
    _inflight_urls.clear()
    _inflight_urls.add("https://example.com/video")
    assert "https://example.com/video" in _inflight_urls
    _inflight_urls.discard("https://example.com/video")
    assert "https://example.com/video" not in _inflight_urls


def test_inflight_key_normalizes_url_and_namespaces_kind():
    # Scheme and host are case-insensitive; path case is preserved (only
    # reddit paths are lowercased, since reddit is case-insensitive).
    assert _inflight_key("video", "HTTPS://Example.com/Video/") == "video:https://example.com/Video"
    assert _inflight_key("audio", "https://example.com/video") != _inflight_key("video", "https://example.com/video")


def test_canonical_url_removes_tracking_params():
    assert (
        canonical_url_for_key("https://www.youtube.com/watch?v=abc&utm_source=x&si=share&t=10")
        == "https://youtube.com/watch?v=abc&t=10"
    )


def test_canonical_url_normalizes_reddit_hosts():
    assert (
        canonical_url_for_key("https://old.reddit.com/r/Test/comments/ABC/?share_id=123")
        == "https://reddit.com/r/test/comments/abc"
    )


def test_canonical_url_preserves_path_case_outside_reddit():
    # Instagram shortcodes are case-sensitive; distinct posts must not share keys.
    assert canonical_url_for_key("https://www.instagram.com/p/AbCdEf/") != canonical_url_for_key(
        "https://www.instagram.com/p/abcdef/"
    )


def test_should_use_aria2c_is_site_aware(monkeypatch):
    monkeypatch.setattr(bot, "USE_ARIA2C", True)
    assert should_use_aria2c("https://youtube.com/watch?v=abc") is False
    assert should_use_aria2c("https://streamable.com/abc123") is True
    assert should_use_aria2c("https://www.instagram.com/p/abc/") is False
    assert should_use_aria2c("https://old.reddit.com/r/test/comments/abc/title/") is False


def test_job_queue_slot_helpers_release_cleanly():
    while _try_reserve_job_slot():
        pass
    running, waiting = _job_queue_status()
    assert running >= 0
    assert waiting >= 0
    _release_job_slot()
    assert _try_reserve_job_slot() is True
    for _ in range(MAX_CONCURRENT_JOBS + MAX_QUEUED_JOBS):
        _release_job_slot()


def test_pipeline_timer_mark_does_not_crash():
    PipelineTimer("test").mark("phase")


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


def _isolate_cache_db(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "CACHE_DB_PATH", str(tmp_path / "test_cache.db"))
    monkeypatch.setattr(bot, "PERSISTENT_CACHE", True)
    monkeypatch.setattr(bot, "_cache_db_conn", None)
    bot._init_persistent_cache()


def test_persist_cache_entry_bool(monkeypatch, tmp_path):
    _isolate_cache_db(monkeypatch, tmp_path)
    asyncio.run(bot._persist_cache_entry_async("test_bool_key", True, "has_video", 3600))
    _flush_cache_writes()
    conn = sqlite3.connect(bot.CACHE_DB_PATH)
    row = conn.execute(
        "SELECT value, cache_type FROM url_cache WHERE key = ?",
        ("test_bool_key",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "1"
    assert row[1] == "has_video"


def test_persist_cache_entry_string(monkeypatch, tmp_path):
    _isolate_cache_db(monkeypatch, tmp_path)
    asyncio.run(
        bot._persist_cache_entry_async("test_str_key", "https://reddit.com/r/test/comments/abc", "shortlink", 3600)
    )
    _flush_cache_writes()
    conn = sqlite3.connect(bot.CACHE_DB_PATH)
    row = conn.execute(
        "SELECT value, cache_type FROM url_cache WHERE key = ?",
        ("test_str_key",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "https://reddit.com/r/test/comments/abc"
    assert row[1] == "shortlink"


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


def test_duration_from_media_info():
    assert duration_from_media_info({"format": {"duration": "12.5"}}) == 12.5
    assert duration_from_media_info({"format": {"duration": "0"}}) is None
    assert duration_from_media_info(None) is None


def test_gif_max_duration():
    assert GIF_MAX_DURATION == 10.0


def test_boost_tier_limits_correct():
    assert BOOST_TIER_LIMITS_MB[0] == 9.5
    assert BOOST_TIER_LIMITS_MB[1] == 9.5
    assert BOOST_TIER_LIMITS_MB[2] == 49.0
    assert BOOST_TIER_LIMITS_MB[3] == 99.0


# ── PR B: release job slot before upload ─────────────────────────────────────


def test_run_download_phase_releases_semaphore_before_return():
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        path = f.name

    async def fake_download():
        assert JOB_SEMAPHORE._value == MAX_CONCURRENT_JOBS - 1
        return path, ""

    async def on_error(msg):
        pass

    async def on_no_video(log_text):
        pass

    async def runner():
        JOB_SEMAPHORE._value = MAX_CONCURRENT_JOBS
        result = await _run_download_phase(fake_download(), on_error, on_no_video)
        assert result == (path, "")
        assert JOB_SEMAPHORE._value == MAX_CONCURRENT_JOBS

    try:
        asyncio.run(runner())
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_run_download_phase_handles_error_and_releases_semaphore():
    async def fake_download():
        raise RuntimeError("boom")

    errors = []

    async def on_error(msg):
        errors.append(msg)

    async def on_no_video(log_text):
        pass

    async def runner():
        JOB_SEMAPHORE._value = MAX_CONCURRENT_JOBS
        result = await _run_download_phase(fake_download(), on_error, on_no_video)
        assert result is None
        assert errors
        assert JOB_SEMAPHORE._value == MAX_CONCURRENT_JOBS

    asyncio.run(runner())


# ── PR D: upload retry with backoff ──────────────────────────────────────────


def _http_exc(status: int) -> discord.HTTPException:
    response = MagicMock()
    response.status = status
    return discord.HTTPException(response, "error")


def test_send_file_with_retry_retries_500_and_succeeds():
    call_count = 0

    async def fake_send(*, file, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _http_exc(500)
        return "sent"

    async def runner():
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"fake")
            path = f.name
        try:
            result = await send_file_with_retry(fake_send, path, send_kwargs={"content": "hi"})
            assert result == "sent"
            assert call_count == 2
        finally:
            os.unlink(path)

    asyncio.run(runner())


def test_send_file_with_retry_does_not_retry_403():
    call_count = 0

    async def fake_send(*, file, **kwargs):
        nonlocal call_count
        call_count += 1
        raise _http_exc(403)

    async def runner():
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"fake")
            path = f.name
        try:
            try:
                await send_file_with_retry(fake_send, path)
            except discord.HTTPException as e:
                assert e.status == 403
            assert call_count == 1
        finally:
            os.unlink(path)

    asyncio.run(runner())


def test_send_file_with_retry_retries_timeout():
    call_count = 0

    async def fake_send(*, file, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise asyncio.TimeoutError()
        return "sent"

    async def runner():
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"fake")
            path = f.name
        try:
            result = await send_file_with_retry(fake_send, path)
            assert result == "sent"
            assert call_count == 2
        finally:
            os.unlink(path)

    asyncio.run(runner())


# ── PR C: info_dict cache ────────────────────────────────────────────────────


def test_ytdlp_info_cache_ttl(monkeypatch):
    monkeypatch.setattr(bot, "_ytdlp_info_cache", {})
    monkeypatch.setattr(bot, "monotonic", lambda: 0.0)
    _set_cached_ytdlp_info("https://youtube.com/watch?v=abc", {"title": "ABC"})
    assert _get_cached_ytdlp_info("https://youtube.com/watch?v=abc") == {"title": "ABC"}

    monkeypatch.setattr(bot, "monotonic", lambda: 10.0 ** 9)
    assert _get_cached_ytdlp_info("https://youtube.com/watch?v=abc") is None


def test_run_ytdlp_with_info_cache_uses_cache_on_hit(monkeypatch, tmp_path):
    _set_cached_ytdlp_info("https://youtube.com/watch?v=abc", {"title": "cached"})

    async def fake_run_subprocess(cmd, timeout=None):
        assert "--load-info-json" in cmd
        return 0, "ok"

    monkeypatch.setattr(bot, "run_subprocess", fake_run_subprocess)

    async def runner():
        code, out = await _run_ytdlp_with_info_cache(
            "https://youtube.com/watch?v=abc", ["yt-dlp"], str(tmp_path), 60
        )
        assert code == 0
        assert out == "ok"

    asyncio.run(runner())


def test_run_ytdlp_with_info_cache_writes_on_miss(monkeypatch, tmp_path):
    async def fake_run_subprocess(cmd, timeout=None):
        assert "--write-info-json" in cmd
        return 0, "ok"

    monkeypatch.setattr(bot, "run_subprocess", fake_run_subprocess)
    info_path = tmp_path / "video.info.json"
    info_path.write_text(json.dumps({"title": "fresh"}))
    monkeypatch.setattr(bot, "_ytdlp_info_cache", {})

    async def runner():
        code, out = await _run_ytdlp_with_info_cache(
            "https://youtube.com/watch?v=xyz", ["yt-dlp"], str(tmp_path), 60
        )
        assert code == 0
        assert _get_cached_ytdlp_info("https://youtube.com/watch?v=xyz") == {"title": "fresh"}

    asyncio.run(runner())


def test_run_ytdlp_with_info_cache_invalidates_on_403(monkeypatch, tmp_path):
    _set_cached_ytdlp_info("https://youtube.com/watch?v=abc", {"title": "cached"})

    async def fake_run_subprocess(cmd, timeout=None):
        if "--load-info-json" in cmd:
            return 1, "HTTP Error 403"
        assert "--write-info-json" in cmd
        return 0, "ok"

    monkeypatch.setattr(bot, "run_subprocess", fake_run_subprocess)

    async def runner():
        code, out = await _run_ytdlp_with_info_cache(
            "https://youtube.com/watch?v=abc", ["yt-dlp"], str(tmp_path), 60
        )
        assert code == 0
        assert out == "ok"
        assert _get_cached_ytdlp_info("https://youtube.com/watch?v=abc") is None

    asyncio.run(runner())


# ── PR A: pre-emptive YouTube quality selection ──────────────────────────────


def test_probe_youtube_quality_steps_down_when_too_big(monkeypatch):
    monkeypatch.setattr(bot, "_ytdlp_info_cache", {})

    async def fake_run_subprocess(cmd, timeout=None):
        return 0, json.dumps({"filesize_approx": 100 * 1024 * 1024})

    monkeypatch.setattr(bot, "run_subprocess", fake_run_subprocess)

    async def runner():
        result = await _probe_youtube_quality(
            "https://youtube.com/watch?v=big", "1080", 10 * 1024 * 1024
        )
        assert result == "720"

    asyncio.run(runner())


def test_probe_youtube_quality_keeps_when_fits(monkeypatch):
    monkeypatch.setattr(bot, "_ytdlp_info_cache", {})

    async def fake_run_subprocess(cmd, timeout=None):
        return 0, json.dumps({"filesize_approx": 5 * 1024 * 1024})

    monkeypatch.setattr(bot, "run_subprocess", fake_run_subprocess)

    async def runner():
        result = await _probe_youtube_quality(
            "https://youtube.com/watch?v=small", "1080", 10 * 1024 * 1024
        )
        assert result == "1080"

    asyncio.run(runner())


def test_probe_youtube_quality_uses_cached_info(monkeypatch):
    monkeypatch.setattr(bot, "_ytdlp_info_cache", {})
    _set_cached_ytdlp_info(
        "https://youtube.com/watch?v=cached", {"filesize_approx": 100 * 1024 * 1024}
    )

    async def runner():
        result = await _probe_youtube_quality(
            "https://youtube.com/watch?v=cached", "1080", 10 * 1024 * 1024
        )
        assert result == "720"

    asyncio.run(runner())


def test_probe_youtube_quality_passes_through_low_qualities():
    async def runner():
        assert await _probe_youtube_quality("https://youtube.com/watch?v=abc", "720", 10) == "720"
        assert await _probe_youtube_quality("https://youtube.com/watch?v=abc", "480", 10) == "480"
        assert await _probe_youtube_quality("https://youtube.com/watch?v=abc", "360", 10) == "360"

    asyncio.run(runner())


def test_reddit_impersonation_args_present_when_curl_cffi_available(monkeypatch):
    monkeypatch.setattr(bot, "CURL_CFFI_AVAILABLE", True)
    assert bot.reddit_impersonation_args() == ["--impersonate", "chrome"]


def test_reddit_impersonation_args_empty_without_curl_cffi(monkeypatch):
    monkeypatch.setattr(bot, "CURL_CFFI_AVAILABLE", False)
    assert bot.reddit_impersonation_args() == []


def test_reddit_no_media_phrases_match_ytdlp_output():
    out = "ERROR: [Reddit] 1uiuvlk: No media found"
    assert any(p in out.lower() for p in bot.REDDIT_NO_MEDIA_PHRASES)


def test_reddit_no_media_phrases_skip_rate_limit_output():
    out = "ERROR: HTTP Error 429: Too Many Requests"
    assert not any(p in out.lower() for p in bot.REDDIT_NO_MEDIA_PHRASES)
