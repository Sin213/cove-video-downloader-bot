import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

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
    key = _inflight_key("video", "https://example.com/video", None)
    _inflight_urls.add(key)
    assert key in _inflight_urls
    _inflight_urls.discard(key)
    assert key not in _inflight_urls


def test_inflight_key_normalizes_url_and_namespaces_kind():
    # Scheme and host are case-insensitive; path case is preserved (only
    # reddit paths are lowercased, since reddit is case-insensitive).
    normalized = _inflight_key("video", "HTTPS://Example.com/Video/", None)
    assert isinstance(normalized, tuple)
    assert normalized[0:2] == ("video", "https://example.com/Video")
    assert normalized == _inflight_key("video", "https://example.com/Video", None)
    keys = {_inflight_key(kind, "https://example.com/video", None) for kind in ("video", "audio", "clip", "gif")}
    assert len(keys) == 4


def test_inflight_key_all_dimensions(monkeypatch):
    url = "https://www.youtube.com/watch?v=abc&utm_source=share"
    guild_a = SimpleNamespace(id=1, premium_tier=0)
    guild_b = SimpleNamespace(id=2, premium_tier=0)
    guild_a_boosted = SimpleNamespace(id=1, premium_tier=3)
    first = _inflight_key("video", url, guild_a, youtube_quality="720")
    assert first == _inflight_key("video", "https://youtube.com/watch?v=abc", guild_a, youtube_quality="720")
    assert first != _inflight_key("video", url, guild_b, youtube_quality="720")
    assert first != _inflight_key("video", url, guild_a_boosted, youtube_quality="720")
    assert first != _inflight_key("video", url, guild_a, youtube_quality="1080")
    assert _inflight_key("clip", url, guild_a, clip_start=1.0, clip_end=2.0) != _inflight_key(
        "clip", url, guild_a, clip_start=2.0, clip_end=3.0
    )
    non_youtube = "https://example.com/video"
    monkeypatch.setattr(bot, "get_youtube_quality", lambda: "720")
    before = _inflight_key("video", non_youtube, guild_a)
    monkeypatch.setattr(bot, "get_youtube_quality", lambda: "1080")
    assert before == _inflight_key("video", non_youtube, guild_a)


@pytest.mark.parametrize("kind", ["clip", "gif"])
def test_clip_gif_request_key_uses_youtube_quality_only_for_youtube(kind):
    youtube = "https://youtube.com/watch?v=abc"
    other = "https://example.com/media"
    bounds = {"clip_start": 1.0, "clip_end": 2.0} if kind == "clip" else {}
    key_720 = _inflight_key(kind, youtube, None, youtube_quality="720", **bounds)
    key_1080 = _inflight_key(kind, youtube, None, youtube_quality="1080", **bounds)
    assert key_720 != key_1080
    assert _inflight_key(kind, other, None, youtube_quality="720", **bounds) == _inflight_key(
        kind, other, None, youtube_quality="1080", **bounds
    )
    if kind == "clip":
        assert key_720 != _inflight_key(
            kind, youtube, None, youtube_quality="720", clip_start=2.0, clip_end=3.0
        )


@pytest.mark.parametrize(
    ("kind", "download_name"),
    [("clip", "download_and_clip"), ("gif", "download_and_gif")],
)
def test_clip_gif_admission_captures_youtube_quality(monkeypatch, kind, download_name):
    calls = []
    monkeypatch.setattr(bot, "get_youtube_quality", lambda: "720")
    monkeypatch.setattr(bot, "_work_key", lambda *args: pytest.fail("clip/GIF entered shared work"))

    async def fake_download(*args, youtube_quality=None):
        calls.append(youtube_quality)
        return None, ""

    monkeypatch.setattr(bot, download_name, fake_download)

    async def noop(*args):
        pass

    async def runner():
        url = "https://youtube.com/watch?v=abc"
        if kind == "clip":
            await bot.process_clip_url(url, None, 1.0, 2.0, noop, noop)
        else:
            await bot.process_gif_url(url, None, noop, noop)

    asyncio.run(runner())
    assert calls == ["720"]
    assert bot._shared_jobs == {}


@pytest.mark.parametrize(
    ("kind", "download_name"),
    [("clip", "download_and_clip"), ("gif", "download_and_gif")],
)
def test_clip_gif_admission_quality_survives_setting_change(monkeypatch, kind, download_name):
    quality = ["720"]
    admitted = asyncio.Event()
    release = asyncio.Event()
    calls = []
    monkeypatch.setattr(bot, "get_youtube_quality", lambda: quality[0])
    real_phase = bot._run_download_phase

    async def delayed_phase(*args, **kwargs):
        admitted.set()
        await release.wait()
        return await real_phase(*args, **kwargs)

    async def fake_download(*args, youtube_quality=None):
        calls.append(youtube_quality)
        return None, ""

    monkeypatch.setattr(bot, "_run_download_phase", delayed_phase)
    monkeypatch.setattr(bot, download_name, fake_download)

    async def noop(*args):
        pass

    async def runner():
        url = "https://youtube.com/watch?v=abc"
        if kind == "clip":
            job = bot.process_clip_url(url, None, 1.0, 2.0, noop, noop)
        else:
            job = bot.process_gif_url(url, None, noop, noop)
        task = asyncio.create_task(job)
        try:
            await admitted.wait()
            quality[0] = "1080"
        finally:
            release.set()
        await task

    asyncio.run(runner())
    assert calls == ["720"]


@pytest.mark.parametrize(
    ("kind", "download_name"),
    [("clip", "download_and_clip"), ("gif", "download_and_gif")],
)
def test_clip_gif_downloader_uses_passed_quality(monkeypatch, tmp_path, kind, download_name):
    url = "https://youtube.com/watch?v=abc"
    calls = []
    monkeypatch.setattr(bot, "get_youtube_quality", lambda: "1080")
    monkeypatch.setattr(bot, "resolve_fixup_url", lambda value: value)

    async def identity(value):
        return value

    async def fake_ytdlp(*args, **kwargs):
        return 1, "offline test failure"

    def fake_format(value, quality=None):
        calls.append((value, quality))
        return None

    monkeypatch.setattr(bot, "resolve_arazu", identity)
    monkeypatch.setattr(bot, "resolve_reddit_shortlink", identity)
    monkeypatch.setattr(bot, "_make_job_tmpdir", lambda: str(tmp_path / "download"))
    monkeypatch.setattr(bot, "_run_ytdlp_with_info_cache", fake_ytdlp)
    monkeypatch.setattr(bot, "youtube_quality_format", fake_format)

    async def runner():
        if kind == "clip":
            await bot.download_and_clip(url, None, 1.0, 2.0, youtube_quality="720")
        else:
            await bot.download_and_gif(url, None, youtube_quality="720")

    asyncio.run(runner())
    assert calls == [(url, "720")]


@pytest.mark.parametrize(
    ("kind", "download_name"),
    [("clip", "download_and_clip"), ("gif", "download_and_gif")],
)
def test_clip_gif_non_youtube_admission_ignores_quality(monkeypatch, kind, download_name):
    calls = []
    monkeypatch.setattr(bot, "get_youtube_quality", lambda: pytest.fail("quality read for non-YouTube"))

    async def fake_download(*args, youtube_quality=None):
        calls.append(youtube_quality)
        return None, ""

    monkeypatch.setattr(bot, download_name, fake_download)

    async def noop(*args):
        pass

    async def runner():
        url = "https://example.com/media"
        if kind == "clip":
            await bot.process_clip_url(url, None, 1.0, 2.0, noop, noop)
        else:
            await bot.process_gif_url(url, None, noop, noop)

    asyncio.run(runner())
    assert calls == [None]


def test_work_key_uses_media_settings_but_not_guild_id():
    guild_a = SimpleNamespace(id=1, premium_tier=0)
    guild_b = SimpleNamespace(id=2, premium_tier=0)
    boosted = SimpleNamespace(id=3, premium_tier=3)
    url = "https://youtube.com/watch?v=abc"
    key = bot._work_key(_inflight_key("video", url, guild_a, youtube_quality="720"))
    assert key == bot._work_key(_inflight_key("video", url, guild_b, youtube_quality="720"))
    assert key != bot._work_key(_inflight_key("video", url, boosted, youtube_quality="720"))
    assert key != bot._work_key(_inflight_key("video", url, guild_b, youtube_quality="1080"))
    plain_url = "https://example.com/media"
    assert bot._work_key(_inflight_key("video", plain_url, guild_a)) != bot._work_key(
        _inflight_key("audio", plain_url, guild_b)
    )


def _inflight_callbacks(events):
    async def on_success(filepath):
        events.append(("success", filepath))

    async def on_error(message):
        events.append(("error", message))

    async def on_no_video(message):
        events.append(("no_video", message))

    return on_success, on_error, None, on_no_video


def test_equivalent_video_work_cross_guild_shares_one_job(monkeypatch, tmp_path):
    guild_a = SimpleNamespace(id=1, premium_tier=0)
    guild_b = SimpleNamespace(id=2, premium_tier=0)
    assert bot.get_target_mb(guild_a) == bot.get_target_mb(guild_b)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    events_a, events_b = [], []

    async def fake_download(url, guild, youtube_quality, target_mb=None):
        with tempfile.NamedTemporaryFile(dir=tmp_path, delete=False) as file:
            filepath = file.name
        calls.append((guild.id, youtube_quality, filepath))
        started.set()
        await release.wait()
        return filepath, ""

    async def no_cleanup(filepath):
        pass

    monkeypatch.setattr(bot, "download_and_compress", fake_download)
    monkeypatch.setattr(bot, "cleanup_tmp", no_cleanup)

    async def runner():
        url = "https://youtube.com/watch?v=abc"
        first = asyncio.create_task(bot.process_url(url, guild_a, *_inflight_callbacks(events_a), youtube_quality="720"))
        await started.wait()
        second = asyncio.create_task(bot.process_url(url, guild_b, *_inflight_callbacks(events_b), youtube_quality="720"))
        try:
            await asyncio.sleep(0)
            assert len(calls) == 1
            assert calls[0][1] == "720"
        finally:
            release.set()
            await asyncio.gather(first, second)
        assert [event[0] for event in events_a] == ["success"]
        assert [event[0] for event in events_b] == ["success"]
        assert events_a[0][1] == events_b[0][1]

    asyncio.run(runner())


def test_equivalent_audio_work_cross_guild_shares_one_job(monkeypatch, tmp_path):
    guild_a = SimpleNamespace(id=1, premium_tier=0)
    guild_b = SimpleNamespace(id=2, premium_tier=0)
    started = asyncio.Event()
    release = asyncio.Event()
    path = tmp_path / "shared"
    path.write_bytes(b"media")
    calls = []
    events_a, events_b = [], []

    async def fake_download(*args, **kwargs):
        calls.append(args)
        started.set()
        await release.wait()
        return str(path), ""

    async def no_cleanup(filepath):
        pass

    monkeypatch.setattr(bot, "download_audio", fake_download)
    monkeypatch.setattr(bot, "cleanup_tmp", no_cleanup)

    def process(guild, events):
        callbacks = _inflight_callbacks(events)
        return bot.process_audio_url("https://example.com/media", guild, *callbacks)

    async def runner():
        first = asyncio.create_task(process(guild_a, events_a))
        await started.wait()
        second = asyncio.create_task(process(guild_b, events_b))
        await asyncio.sleep(0)
        assert len(calls) == 1
        release.set()
        await asyncio.gather(first, second)
        assert events_a == [("success", str(path))]
        assert events_b == [("success", str(path))]

    asyncio.run(runner())


def test_shared_video_uses_admission_target_after_creator_tier_changes(monkeypatch, tmp_path):
    guild_a = SimpleNamespace(id=1, premium_tier=0)
    guild_b = SimpleNamespace(id=2, premium_tier=0)
    admitted_mb = bot.get_target_mb(guild_a)
    source, compression_calls = _fake_video_pipeline(monkeypatch, tmp_path, 20 * 1024 * 1024)
    real_download = bot.download_and_compress
    used_targets = []
    events_a, events_b = [], []

    async def record_download(url, guild, youtube_quality, target_mb):
        used_targets.append(target_mb)
        return await real_download(url, guild, youtube_quality, target_mb=target_mb)

    async def no_cleanup(filepath):
        pass

    monkeypatch.setattr(bot, "download_and_compress", record_download)
    monkeypatch.setattr(bot, "cleanup_tmp", no_cleanup)

    async def runner():
        permits = bot.JOB_SEMAPHORE._value
        for _ in range(permits):
            await bot.JOB_SEMAPHORE.acquire()
        first = second = None
        try:
            first = asyncio.create_task(bot.process_url("https://example.com/video", guild_a, *_inflight_callbacks(events_a)))
            second = asyncio.create_task(bot.process_url("https://example.com/video", guild_b, *_inflight_callbacks(events_b)))
            for _ in range(100):
                if len(bot._shared_jobs) == 1 and next(iter(bot._shared_jobs.values())).subscribers == 2:
                    break
                await asyncio.sleep(0)
            assert len(bot._shared_jobs) == 1
            assert next(iter(bot._shared_jobs.values())).subscribers == 2
            assert used_targets == []
            guild_a.premium_tier = 3
        finally:
            for _ in range(permits):
                bot.JOB_SEMAPHORE.release()
        await asyncio.gather(first, second)

    asyncio.run(runner())
    assert used_targets == [admitted_mb]
    assert compression_calls == [(str(source), str(tmp_path / "compressed.mp4"), admitted_mb)]
    assert [event[0] for event in events_a] == ["success"]
    assert [event[0] for event in events_b] == ["success"]


@pytest.mark.parametrize(
    ("kind", "download_name"),
    [("clip", "download_and_clip"), ("gif", "download_and_gif")],
)
def test_clip_and_gif_only_reject_exact_duplicate_requests(monkeypatch, tmp_path, kind, download_name):
    guild_a = SimpleNamespace(id=1, premium_tier=0)
    guild_b = SimpleNamespace(id=2, premium_tier=0)
    started = asyncio.Event()
    both_started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    cleaned = []
    events_a, events_b, duplicate_events = [], [], []

    async def fake_download(*args, **kwargs):
        path = tmp_path / f"{len(calls)}.mp4"
        path.write_bytes(b"media")
        calls.append(args)
        started.set()
        if len(calls) == 2:
            both_started.set()
        await release.wait()
        return str(path), ""

    async def cleanup(filepath):
        cleaned.append(filepath)

    monkeypatch.setattr(bot, download_name, fake_download)
    monkeypatch.setattr(bot, "cleanup_tmp", cleanup)

    def process(guild, events):
        callbacks = _inflight_callbacks(events)
        if kind == "clip":
            return bot.process_clip_url("https://example.com/media", guild, 1.0, 2.0,
                                        callbacks[0], callbacks[1], callbacks[3])
        return bot.process_gif_url("https://example.com/media", guild,
                                   callbacks[0], callbacks[1], callbacks[3])

    async def runner():
        before = bot._queued_jobs
        first = asyncio.create_task(process(guild_a, events_a))
        await started.wait()
        second = asyncio.create_task(process(guild_b, events_b))
        duplicate = asyncio.create_task(process(guild_a, duplicate_events))
        try:
            await asyncio.wait_for(both_started.wait(), timeout=1)
            await asyncio.wait_for(duplicate, timeout=1)
            assert len(calls) == 2
            assert bot._queued_jobs == before + 2
            assert bot._shared_jobs == {}
            assert duplicate_events == [("no_video", bot.INFLIGHT_MARKER)]
        finally:
            release.set()
            await asyncio.gather(first, second)
        assert [event[0] for event in events_a] == ["success"]
        assert [event[0] for event in events_b] == ["success"]
        assert len(cleaned) == 2
        assert bot._queued_jobs == before

    asyncio.run(runner())


def test_different_target_mb_uses_separate_video_jobs(monkeypatch, tmp_path):
    guild_a = SimpleNamespace(id=1, premium_tier=0)
    guild_b = SimpleNamespace(id=2, premium_tier=3)
    assert bot.get_target_mb(guild_a) != bot.get_target_mb(guild_b)
    started = asyncio.Event()
    both_started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    events_a, events_b = [], []

    async def fake_download(url, guild, youtube_quality, target_mb=None):
        path = tmp_path / f"{guild.id}.mp4"
        path.write_bytes(b"video")
        calls.append(bot.get_target_mb(guild))
        started.set()
        if len(calls) == 2:
            both_started.set()
        await release.wait()
        return str(path), ""

    async def no_cleanup(filepath):
        pass

    monkeypatch.setattr(bot, "download_and_compress", fake_download)
    monkeypatch.setattr(bot, "cleanup_tmp", no_cleanup)

    async def runner():
        first = asyncio.create_task(bot.process_url("https://example.com/media", guild_a, *_inflight_callbacks(events_a)))
        await started.wait()
        second = asyncio.create_task(bot.process_url("https://example.com/media", guild_b, *_inflight_callbacks(events_b)))
        try:
            await asyncio.wait_for(both_started.wait(), timeout=1)
        finally:
            release.set()
            await asyncio.gather(first, second)
        assert calls == [bot.get_target_mb(guild_a), bot.get_target_mb(guild_b)]
        assert [event[0] for event in events_a] == ["success"]
        assert [event[0] for event in events_b] == ["success"]

    asyncio.run(runner())


def test_different_clip_bounds_use_separate_jobs(monkeypatch, tmp_path):
    guild = SimpleNamespace(id=1, premium_tier=0)
    started = asyncio.Event()
    both_started = asyncio.Event()
    release = asyncio.Event()
    bounds = []
    events_a, events_b = [], []

    async def fake_download(url, guild, start, end, youtube_quality=None):
        path = tmp_path / f"{end}.mp4"
        path.write_bytes(b"video")
        bounds.append((start, end))
        started.set()
        if len(bounds) == 2:
            both_started.set()
        await release.wait()
        return str(path), ""

    async def no_cleanup(filepath):
        pass

    monkeypatch.setattr(bot, "download_and_clip", fake_download)
    monkeypatch.setattr(bot, "cleanup_tmp", no_cleanup)

    async def runner():
        cb_a = _inflight_callbacks(events_a)
        cb_b = _inflight_callbacks(events_b)
        first = asyncio.create_task(bot.process_clip_url("https://example.com/media", guild, 1, 2,
                                                          cb_a[0], cb_a[1], cb_a[3]))
        await started.wait()
        second = asyncio.create_task(bot.process_clip_url("https://example.com/media", guild, 1, 3,
                                                           cb_b[0], cb_b[1], cb_b[3]))
        try:
            await asyncio.wait_for(both_started.wait(), timeout=1)
        finally:
            release.set()
            await asyncio.gather(first, second)
        assert bounds == [(1, 2), (1, 3)]
        assert [event[0] for event in events_a] == ["success"]
        assert [event[0] for event in events_b] == ["success"]

    asyncio.run(runner())


def test_shared_video_waits_for_both_deliveries_before_cleanup(monkeypatch, tmp_path):
    guild_a = SimpleNamespace(id=1, premium_tier=0)
    guild_b = SimpleNamespace(id=2, premium_tier=0)
    path = tmp_path / "shared.mp4"
    path.write_bytes(b"video")
    started = asyncio.Event()
    release_download = asyncio.Event()
    release_upload = asyncio.Event()
    cleaned = []
    calls = []
    errors = []

    async def fake_download(*args, **kwargs):
        calls.append(args)
        started.set()
        await release_download.wait()
        return str(path), ""

    async def first_success(filepath):
        assert filepath == str(path)
        await release_upload.wait()

    async def second_success(filepath):
        assert filepath == str(path)
        raise RuntimeError("upload failed")

    async def on_error(message):
        errors.append(message)

    async def cleanup(filepath):
        cleaned.append(filepath)

    monkeypatch.setattr(bot, "download_and_compress", fake_download)
    monkeypatch.setattr(bot, "cleanup_tmp", cleanup)

    async def runner():
        before = bot._queued_jobs
        first = asyncio.create_task(bot.process_url("https://example.com/media", guild_a, first_success, on_error))
        await started.wait()
        second = asyncio.create_task(bot.process_url("https://example.com/media", guild_b, second_success, on_error))
        await asyncio.sleep(0)
        assert len(calls) == 1
        assert bot._queued_jobs == before + 1
        assert next(iter(bot._shared_jobs.values())).task in bot._active_tasks
        release_download.set()
        await second
        assert errors and "upload failed" in errors[0]
        assert cleaned == []
        assert bot._queued_jobs == before + 1
        release_upload.set()
        await first
        assert cleaned == [str(path)]
        assert bot._queued_jobs == before

    asyncio.run(runner())


@pytest.mark.parametrize(
    ("log_text", "expected"),
    [
        ("[NOVIDEO]", "no_video"),
        ("[TOOBIG] 25MB", "too_big"),
        ("[ERROR] Access denied", "error"),
    ],
)
def test_shared_video_failure_notifies_each_guild(monkeypatch, log_text, expected):
    guild_a = SimpleNamespace(id=1, premium_tier=0)
    guild_b = SimpleNamespace(id=2, premium_tier=0)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    events_a, events_b = [], []

    async def fake_download(*args, **kwargs):
        calls.append(args)
        started.set()
        await release.wait()
        return None, log_text

    monkeypatch.setattr(bot, "download_and_compress", fake_download)

    async def runner():
        callbacks_a = list(_inflight_callbacks(events_a))
        if expected == "too_big":
            async def on_too_big(message):
                events_a.append(("too_big", message))
            callbacks_a[2] = on_too_big
        first = asyncio.create_task(bot.process_url("https://example.com/media", guild_a, *callbacks_a))
        await started.wait()
        second = asyncio.create_task(bot.process_url("https://example.com/media", guild_b, *_inflight_callbacks(events_b)))
        await asyncio.sleep(0)
        assert len(calls) == 1
        release.set()
        await asyncio.gather(first, second)
        assert [event[0] for event in events_a] == [expected]
        assert [event[0] for event in events_b] == ["error" if expected == "too_big" else expected]
        assert bot._shared_jobs == {}

    asyncio.run(runner())


def test_shared_video_join_does_not_reserve_another_queue_slot(monkeypatch, tmp_path):
    guild_a = SimpleNamespace(id=1, premium_tier=0)
    guild_b = SimpleNamespace(id=2, premium_tier=0)
    path = tmp_path / "shared.mp4"
    path.write_bytes(b"video")
    started = asyncio.Event()
    release = asyncio.Event()
    reservations = []
    events_a, events_b = [], []

    async def fake_download(*args, **kwargs):
        started.set()
        await release.wait()
        return str(path), ""

    def reserve():
        reservations.append(True)
        return len(reservations) == 1

    async def no_cleanup(filepath):
        pass

    monkeypatch.setattr(bot, "download_and_compress", fake_download)
    monkeypatch.setattr(bot, "_try_reserve_job_slot", reserve)
    monkeypatch.setattr(bot, "_release_job_slot", lambda: None)
    monkeypatch.setattr(bot, "cleanup_tmp", no_cleanup)

    async def runner():
        first = asyncio.create_task(bot.process_url("https://example.com/media", guild_a, *_inflight_callbacks(events_a)))
        await started.wait()
        second = asyncio.create_task(bot.process_url("https://example.com/media", guild_b, *_inflight_callbacks(events_b)))
        await asyncio.sleep(0)
        assert len(reservations) == 1
        release.set()
        await asyncio.gather(first, second)
        assert [event[0] for event in events_a] == ["success"]
        assert [event[0] for event in events_b] == ["success"]

    asyncio.run(runner())


def test_shared_video_survives_one_subscriber_cancellation(monkeypatch, tmp_path):
    guild_a = SimpleNamespace(id=1, premium_tier=0)
    guild_b = SimpleNamespace(id=2, premium_tier=0)
    path = tmp_path / "shared.mp4"
    path.write_bytes(b"video")
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    cleaned = []
    events_b = []

    async def fake_download(*args, **kwargs):
        calls.append(args)
        started.set()
        await release.wait()
        return str(path), ""

    async def cleanup(filepath):
        cleaned.append(filepath)

    monkeypatch.setattr(bot, "download_and_compress", fake_download)
    monkeypatch.setattr(bot, "cleanup_tmp", cleanup)

    async def runner():
        first = asyncio.create_task(bot.process_url("https://example.com/media", guild_a, *_inflight_callbacks([])))
        await started.wait()
        second = asyncio.create_task(bot.process_url("https://example.com/media", guild_b, *_inflight_callbacks(events_b)))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert cleaned == []
        release.set()
        await second
        assert len(calls) == 1
        assert events_b == [("success", str(path))]
        assert cleaned == [str(path)]

    asyncio.run(runner())


def test_shared_video_finishes_after_all_subscribers_cancel(monkeypatch, tmp_path):
    guild_a = SimpleNamespace(id=1, premium_tier=0)
    guild_b = SimpleNamespace(id=2, premium_tier=0)
    path = tmp_path / "shared.mp4"
    path.write_bytes(b"video")
    started = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()
    cleanup_calls = []
    calls = []

    async def fake_download(*args, **kwargs):
        calls.append(args)
        started.set()
        await release.wait()
        return str(path), ""

    async def cleanup(filepath):
        cleanup_calls.append(filepath)
        cleaned.set()

    monkeypatch.setattr(bot, "download_and_compress", fake_download)
    monkeypatch.setattr(bot, "cleanup_tmp", cleanup)

    async def runner():
        before = bot._queued_jobs
        first = asyncio.create_task(bot.process_url("https://example.com/media", guild_a, *_inflight_callbacks([])))
        await started.wait()
        second = asyncio.create_task(bot.process_url("https://example.com/media", guild_b, *_inflight_callbacks([])))
        await asyncio.sleep(0)
        assert len(bot._shared_jobs) == 1
        job = next(iter(bot._shared_jobs.values()))
        assert job.task in bot._active_tasks
        assert job.subscribers == 2
        first.cancel()
        second.cancel()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert job.subscribers == 0
        assert not job.task.done()
        assert not job.task.cancelled()
        assert bot._shared_jobs
        assert bot._queued_jobs == before + 1
        assert cleanup_calls == []
        release.set()
        await asyncio.wait_for(job.task, timeout=1)
        await asyncio.wait_for(cleaned.wait(), timeout=1)
        assert cleanup_calls == [str(path)]
        assert bot._shared_jobs == {}
        assert bot._queued_jobs == before
        assert len(calls) == 1

    asyncio.run(runner())


def test_shared_job_releases_slot_before_pending_finalizer_is_cancelled(monkeypatch, tmp_path):
    guild = SimpleNamespace(id=1, premium_tier=0)
    path = tmp_path / "shared.mp4"
    path.write_bytes(b"video")
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled_finalizers = []
    original_spawn = bot.spawn_tracked
    spawned = 0

    async def fake_download(*args, **kwargs):
        started.set()
        await release.wait()
        return str(path), ""

    def cancel_pending_finalizer(coro):
        nonlocal spawned
        spawned += 1
        task = original_spawn(coro)
        if spawned == 2:
            cancelled_finalizers.append(task)
            task.cancel()
        return task

    monkeypatch.setattr(bot, "download_and_compress", fake_download)
    monkeypatch.setattr(bot, "spawn_tracked", cancel_pending_finalizer)

    async def runner():
        before = bot._queued_jobs
        subscriber = asyncio.create_task(bot.process_url("https://example.com/video", guild, *_inflight_callbacks([])))
        await started.wait()
        job = next(iter(bot._shared_jobs.values()))
        subscriber.cancel()
        with pytest.raises(asyncio.CancelledError):
            await subscriber
        assert job.subscribers == 0
        release.set()
        await job.task
        await asyncio.sleep(0)
        assert len(cancelled_finalizers) == 1
        await asyncio.gather(*cancelled_finalizers, return_exceptions=True)
        assert bot._shared_jobs == {}
        assert bot._queued_jobs == before

    asyncio.run(runner())


def test_inflight_explicit_youtube_qualities_both_succeed(monkeypatch, tmp_path):
    guild = SimpleNamespace(id=1, premium_tier=0)
    started = asyncio.Event()
    both_started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    events_a, events_b = [], []

    async def fake_download(url, guild, youtube_quality, target_mb=None):
        with tempfile.NamedTemporaryFile(dir=tmp_path, delete=False) as file:
            filepath = file.name
        calls.append(youtube_quality)
        started.set()
        if len(calls) == 2:
            both_started.set()
        await release.wait()
        return filepath, ""

    async def no_cleanup(filepath):
        pass

    monkeypatch.setattr(bot, "download_and_compress", fake_download)
    monkeypatch.setattr(bot, "cleanup_tmp", no_cleanup)

    async def runner():
        url = "https://youtube.com/watch?v=abc"
        first = asyncio.create_task(bot.process_url(url, guild, *_inflight_callbacks(events_a), youtube_quality="720"))
        await started.wait()
        second = asyncio.create_task(bot.process_url(url, guild, *_inflight_callbacks(events_b), youtube_quality="1080"))
        try:
            await asyncio.wait_for(both_started.wait(), timeout=1)
            assert calls == ["720", "1080"]
        finally:
            release.set()
            await asyncio.gather(first, second)
        assert [event[0] for event in events_a] == ["success"]
        assert [event[0] for event in events_b] == ["success"]

    asyncio.run(runner())


def test_inflight_default_youtube_quality_captured_at_admission(monkeypatch, tmp_path):
    guild = SimpleNamespace(id=1, premium_tier=0)
    current_quality = ["720"]
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    events = []
    monkeypatch.setattr(bot, "get_youtube_quality", lambda: current_quality[0])

    async def fake_download(url, guild, youtube_quality, target_mb=None):
        with tempfile.NamedTemporaryFile(dir=tmp_path, delete=False) as file:
            filepath = file.name
        calls.append(youtube_quality)
        started.set()
        await release.wait()
        return filepath, ""

    async def no_cleanup(filepath):
        pass

    monkeypatch.setattr(bot, "download_and_compress", fake_download)
    monkeypatch.setattr(bot, "cleanup_tmp", no_cleanup)

    async def runner():
        task = asyncio.create_task(bot.process_url("https://youtube.com/watch?v=abc", guild, *_inflight_callbacks(events)))
        await started.wait()
        current_quality[0] = "1080"
        release.set()
        await task
        assert calls == ["720"]
        assert [event[0] for event in events] == ["success"]

    asyncio.run(runner())


def test_inflight_exact_duplicate_rejected_then_admitted(monkeypatch, tmp_path):
    guild = SimpleNamespace(id=1, premium_tier=0)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []
    events_a, events_b, events_c = [], [], []

    async def fake_download(url, guild, youtube_quality, target_mb=None):
        with tempfile.NamedTemporaryFile(dir=tmp_path, delete=False) as file:
            filepath = file.name
        calls.append(filepath)
        started.set()
        await release.wait()
        return filepath, ""

    async def no_cleanup(filepath):
        pass

    monkeypatch.setattr(bot, "download_and_compress", fake_download)
    monkeypatch.setattr(bot, "cleanup_tmp", no_cleanup)

    async def runner():
        url = "https://youtube.com/watch?v=abc"
        first = asyncio.create_task(bot.process_url(url, guild, *_inflight_callbacks(events_a), youtube_quality="720"))
        await started.wait()
        second = asyncio.create_task(bot.process_url(url, guild, *_inflight_callbacks(events_b), youtube_quality="720"))
        try:
            await asyncio.wait_for(second, timeout=0.5)
            assert len(calls) == 1
            assert events_b == [("no_video", bot.INFLIGHT_MARKER)]
        finally:
            release.set()
            await first
        assert [event[0] for event in events_a] == ["success"]
        await bot.process_url(url, guild, *_inflight_callbacks(events_c), youtube_quality="720")
        assert len(calls) == 2
        assert [event[0] for event in events_c] == ["success"]

    asyncio.run(runner())


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
    assert BOOST_TIER_LIMITS_MB[0] == 19.5
    assert BOOST_TIER_LIMITS_MB[1] == 19.5
    assert BOOST_TIER_LIMITS_MB[2] == 49.0
    assert BOOST_TIER_LIMITS_MB[3] == 99.0
    for tier, limit in {0: 19.5, 1: 19.5, 2: 49.0, 3: 99.0}.items():
        assert bot.get_target_mb(SimpleNamespace(premium_tier=tier)) == limit


def test_unknown_boost_tier_uses_default_limit():
    for tier in (None, 99):
        assert bot.get_target_mb(SimpleNamespace(premium_tier=tier)) == BOOST_TIER_LIMITS_MB[0]


def _fake_video_pipeline(monkeypatch, tmp_path, source_size):
    source = tmp_path / "source.mp4"
    compression_calls = []

    async def fake_download(url, cmd, tmp, timeout, **kwargs):
        assert tmp == str(tmp_path)
        with source.open("wb") as output:
            output.truncate(source_size)
        return 0, "downloaded"

    async def fake_media_info(filepath):
        assert filepath == str(source)
        return {"format": {"duration": "10.0"}, "streams": []}

    async def fake_compress(src, dest, target_mb, duration=None):
        compression_calls.append((src, dest, target_mb))
        Path(dest).write_bytes(b"compressed")
        return True, "0.01 MB"

    monkeypatch.setattr(bot, "_make_job_tmpdir", lambda: str(tmp_path))
    monkeypatch.setattr(bot, "_run_ytdlp_with_info_cache", fake_download)
    monkeypatch.setattr(bot, "get_media_info", fake_media_info)
    monkeypatch.setattr(bot, "discord_mp4_compatibility", lambda info, path: (False, "fake"))
    monkeypatch.setattr(bot, "compress_to_target", fake_compress)
    return source, compression_calls


def test_default_limit_15_mib_uploads_original_without_compression(monkeypatch, tmp_path):
    source, compression_calls = _fake_video_pipeline(monkeypatch, tmp_path, 15 * 1024 * 1024)

    delivered, _ = asyncio.run(bot.download_and_compress("https://example.com/video", None))

    assert compression_calls == []
    assert delivered == str(source)
    assert Path(delivered).name != "compressed.mp4"


def test_default_limit_20_mib_compresses_to_19_5_mb(monkeypatch, tmp_path):
    source, compression_calls = _fake_video_pipeline(monkeypatch, tmp_path, 20 * 1024 * 1024)

    delivered, _ = asyncio.run(bot.download_and_compress("https://example.com/video", None))

    assert compression_calls == [(str(source), str(tmp_path / "compressed.mp4"), 19.5)]
    assert delivered == str(tmp_path / "compressed.mp4")


def test_default_limit_gif_uses_eight_seconds_and_380px(monkeypatch, tmp_path):
    commands = []
    source = tmp_path / "source.mp4"
    destination = tmp_path / "converted.gif"
    source.write_bytes(b"video")

    async def fake_run_subprocess(cmd, **kwargs):
        commands.append(cmd)
        Path(cmd[-1]).write_bytes(b"gif")
        return 0, "converted"

    async def fake_duration(filepath):
        assert filepath == str(source)
        return 10.0

    monkeypatch.setattr(bot, "get_duration", fake_duration)
    monkeypatch.setattr(bot, "run_subprocess", fake_run_subprocess)

    ok, _ = asyncio.run(bot.convert_to_gif(str(source), str(destination), bot.get_target_mb(None)))

    assert ok
    assert commands[0][commands[0].index("-t") + 1] == "8.0"
    assert "fps=12,scale=380" in commands[0][commands[0].index("-vf") + 1]


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


def test_run_subprocess_bounded_capture_keeps_terminal_error():
    script = (
        "import sys; sys.stdout.write('x' * 600000); "
        "sys.stdout.write('HTTP Error 403: TERMINAL-MARKER'); sys.exit(1)"
    )

    async def runner():
        return await bot.run_subprocess([sys.executable, "-c", script], timeout=30)

    code, out = asyncio.run(runner())
    assert code == 1
    assert len(out.encode()) <= bot.MAX_SUBPROCESS_OUTPUT_BYTES + 100
    assert "HTTP Error 403: TERMINAL-MARKER" in out


def test_run_subprocess_bounded_capture_keeps_early_error_marker():
    script = (
        "import sys; sys.stdout.write('HTTP Error 403: EARLY-MARKER'); "
        "sys.stdout.write('x' * 600000); sys.exit(0)"
    )

    async def runner():
        return await bot.run_subprocess([sys.executable, "-c", script], timeout=30)

    code, out = asyncio.run(runner())
    assert code == 0
    assert len(out.encode()) <= bot.MAX_SUBPROCESS_OUTPUT_BYTES + 100
    assert "HTTP Error 403: EARLY-MARKER" in out


def test_run_subprocess_bounded_capture_keeps_middle_error_marker():
    script = (
        "import sys; sys.stdout.write('a' * 300000); "
        "sys.stdout.write('HTTP Error 403: MIDDLE'); "
        "sys.stdout.write('b' * 300000); sys.exit(0)"
    )

    async def runner():
        return await bot.run_subprocess([sys.executable, "-c", script], timeout=30)

    code, out = asyncio.run(runner())
    assert code == 0
    assert len(out.encode()) <= bot.MAX_SUBPROCESS_OUTPUT_BYTES + 100
    assert "HTTP Error 403" in out


def test_run_subprocess_truncation_seam_cannot_synthesize_403():
    head_cap = bot.MAX_SUBPROCESS_OUTPUT_BYTES // 2
    tail_cap = bot.MAX_SUBPROCESS_OUTPUT_BYTES - head_cap
    script = (
        "import sys; "
        f"sys.stdout.write('x' * {head_cap - 11}); "
        "sys.stdout.write('HTTP Error '); "
        "sys.stdout.write('z' * 100); "
        "sys.stdout.write('403'); "
        f"sys.stdout.write('y' * {tail_cap - 3}); sys.exit(0)"
    )

    async def runner():
        return await bot.run_subprocess([sys.executable, "-c", script], timeout=30)

    code, out = asyncio.run(runner())
    assert code == 0
    assert "HTTP Error 403" not in out
