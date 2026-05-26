import asyncio
from pathlib import Path

import pytest

from bot import (
    extract_supported_url,
    instagram_is_image_post,
    process_url,
    reddit_api_url,
    reddit_gif_url_from_log,
    reddit_image_url_from_log,
    reddit_image_url_from_post,
    reddit_media_gif_url_from_text,
    reddit_media_image_url_from_text,
    rewrite_instagram_image_url,
    send_reddit_image_repost,
    send_reddit_gif_repost,
    send_instagram_image_rewrite,
    user_facing_download_error,
    user_facing_upload_error,
    _instagram_entry_has_video,
    _is_instagram_image_entry,
    INSTAGRAM_IMAGE_MARKER,
    _sanitize_error_line,
    _sanitize_filename,
    _check_user_rate_limit,
    USER_RATE_LIMIT,
    _user_request_times,
)


class FakeChannel:
    def __init__(self, events, send_error=None):
        self.events = events
        self.send_error = send_error

    async def send(self, content=None, **kwargs):
        if "file" in kwargs:
            self.events.append(("send_file", Path(kwargs["file"].fp.name).name))
            return
        self.events.append(("send", content))
        if self.send_error:
            raise self.send_error

    def permissions_for(self, member):
        return member.permissions


class FakeMessage:
    def __init__(self, events, send_error=None, delete_error=None, guild=None):
        self.events = events
        self.channel = FakeChannel(events, send_error)
        self.delete_error = delete_error
        self.guild = guild

    async def delete(self):
        self.events.append(("delete", None))
        if self.delete_error:
            raise self.delete_error


class FakeDiscordError(Exception):
    pass


class FakePermissions:
    def __init__(self, embed_links=True):
        self.embed_links = embed_links


class FakeMember:
    def __init__(self, permissions):
        self.permissions = permissions


class FakeGuild:
    def __init__(self, permissions):
        self.me = FakeMember(permissions)


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


def test_filename_preserves_extension_when_stem_ends_with_dot():
    assert _sanitize_filename("What Are You..mp4") == "What Are You_.mp4"


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


def test_rewrite_instagram_image_url_uses_embed_proxy_host():
    result = rewrite_instagram_image_url(
        "https://www.instagram.com/p/abc123/?img_index=1",
        f"{INSTAGRAM_IMAGE_MARKER}\n[NOVIDEO]",
    )
    assert result == "https://www.kkinstagram.com/p/abc123/?img_index=1"


def test_rewrite_instagram_image_url_requires_image_signal():
    result = rewrite_instagram_image_url(
        "https://www.instagram.com/p/abc123/?img_index=1",
        "[NOVIDEO]\nHTTP Error 429: Too Many Requests",
    )
    assert result is None


def test_rewrite_instagram_image_url_rejects_non_instagram_url():
    result = rewrite_instagram_image_url(
        "https://x.com/example/status/123",
        f"{INSTAGRAM_IMAGE_MARKER}\n[NOVIDEO]",
    )
    assert result is None


def test_rewrite_instagram_image_url_rejects_non_post_instagram_url():
    result = rewrite_instagram_image_url(
        "https://www.instagram.com/reel/abc123/",
        f"{INSTAGRAM_IMAGE_MARKER}\n[NOVIDEO]",
    )
    assert result is None


def test_instagram_image_entry_rejects_video_metadata():
    assert _is_instagram_image_entry({"duration": 12.3, "ext": "mp4"}) is False


def test_instagram_image_entry_rejects_thumbnail_only_metadata():
    assert _is_instagram_image_entry({"duration": None, "thumbnail": "https://cdn/x.jpg"}) is False


def test_instagram_image_entry_accepts_image_metadata():
    assert _is_instagram_image_entry({"duration": None, "ext": "jpg"}) is True


def test_instagram_image_entry_accepts_image_playlist_with_null_placeholder():
    assert _is_instagram_image_entry(
        {"entries": [{"duration": None, "ext": "jpg"}, None]}
    ) is True


def test_instagram_image_entry_rejects_all_null_playlist():
    assert _is_instagram_image_entry({"entries": [None]}) is False


def test_instagram_video_entry_accepts_format_mp4_metadata():
    assert _instagram_entry_has_video(
        {"entries": [{"duration": None, "ext": "jpg"}, {"formats": [{"ext": "mp4"}]}]}
    ) is True


def test_instagram_image_text_only_metadata_with_image_dicts_and_none_classifies(monkeypatch):
    async def fake_run_subprocess(cmd):
        assert "--no-playlist" not in cmd
        assert "--ignore-no-formats" in cmd
        return 0, '{"entries": [{"duration": null, "ext": "jpg"}, null]}'

    monkeypatch.setattr("bot.run_subprocess", fake_run_subprocess)

    assert asyncio.run(instagram_is_image_post("https://www.instagram.com/p/abc123/")) is True


def test_instagram_image_post_rejects_all_null_playlist_metadata(monkeypatch):
    async def fake_run_subprocess(cmd):
        assert "--no-playlist" not in cmd
        assert "--ignore-no-formats" in cmd
        return 0, '{"entries": [null, null]}'

    monkeypatch.setattr("bot.run_subprocess", fake_run_subprocess)

    assert asyncio.run(instagram_is_image_post("https://www.instagram.com/p/abc123/")) is False


def test_instagram_image_post_accepts_query_string_post_url(monkeypatch):
    async def fake_run_subprocess(cmd):
        assert "--no-playlist" not in cmd
        assert "--ignore-no-formats" in cmd
        return 0, '{"duration": null, "ext": "jpg"}'

    monkeypatch.setattr("bot.run_subprocess", fake_run_subprocess)

    assert asyncio.run(instagram_is_image_post("https://www.instagram.com/p/abc123/?img_index=1")) is True


@pytest.mark.parametrize(
    "output",
    [
        "[NOVIDEO]",
        "ERROR: HTTP Error 429: Too Many Requests",
        "ERROR: This account is private",
        "ERROR: The requested content is not available, it may have been deleted",
        "ERROR: Login required",
        "ERROR: Unable to download webpage: timed out",
    ],
)
def test_instagram_image_post_rejects_non_image_no_video_errors(monkeypatch, output):
    async def fake_run_subprocess(cmd):
        return 1, output

    monkeypatch.setattr("bot.run_subprocess", fake_run_subprocess)

    assert asyncio.run(instagram_is_image_post("https://www.instagram.com/p/abc123/")) is False


def test_instagram_image_post_rejects_no_video_error_without_json(monkeypatch):
    async def fake_run_subprocess(cmd):
        return 1, "ERROR: [Instagram] abc123: There is no video in this post"

    monkeypatch.setattr("bot.run_subprocess", fake_run_subprocess)

    assert asyncio.run(instagram_is_image_post("https://www.instagram.com/p/abc123/")) is False


def test_instagram_image_post_rejects_no_video_error_with_rate_limit(monkeypatch):
    async def fake_run_subprocess(cmd):
        return 1, "ERROR: HTTP Error 429: Too Many Requests. There is no video in this post"

    monkeypatch.setattr("bot.run_subprocess", fake_run_subprocess)

    assert asyncio.run(instagram_is_image_post("https://www.instagram.com/p/abc123/")) is False


def test_instagram_image_post_rejects_non_post_url(monkeypatch):
    async def fail_run_subprocess(cmd):
        raise AssertionError("yt-dlp should not run for unsupported Instagram paths")

    monkeypatch.setattr("bot.run_subprocess", fail_run_subprocess)

    assert asyncio.run(instagram_is_image_post("https://www.instagram.com/reel/abc123/")) is False


def test_rewritten_instagram_url_is_not_auto_downloaded():
    assert extract_supported_url("https://kkinstagram.com/p/abc123/") is None


def test_direct_gif_url_is_not_auto_downloaded():
    assert extract_supported_url("https://i.redd.it/isd965xyhwsf1.gif") is None


def test_reddit_media_url_extracts_direct_gif():
    assert (
        reddit_media_gif_url_from_text(
            "ERROR: Unsupported URL: "
            "https://www.reddit.com/media?url=https%3A%2F%2Fi.redd.it%2Fisd965xyhwsf1.gif"
        )
        == "https://i.redd.it/isd965xyhwsf1.gif"
    )


def test_reddit_media_url_extracts_direct_image():
    assert (
        reddit_media_image_url_from_text(
            "ERROR: Unsupported URL: "
            "https://www.reddit.com/media?url=https%3A%2F%2Fi.redd.it%2Fexample.jpg"
        )
        == "https://i.redd.it/example.jpg"
    )


def test_reddit_api_url_normalizes_old_reddit_to_www():
    assert (
        reddit_api_url("https://old.reddit.com/r/aliens/comments/1tnlhwb/title/?share_id=abc")
        == "https://www.reddit.com/r/aliens/comments/1tnlhwb/title.json?limit=1"
    )


def test_reddit_image_url_from_gallery_post():
    assert (
        reddit_image_url_from_post(
            {
                "media_metadata": {
                    "abc": {
                        "s": {
                            "u": "https://preview.redd.it/example.jpg?width=960&amp;format=pjpg&amp;auto=webp&amp;s=abc"
                        }
                    }
                }
            }
        )
        == "https://preview.redd.it/example.jpg?width=960&format=pjpg&auto=webp&s=abc"
    )


def test_reddit_image_url_from_gallery_uses_gallery_order():
    assert (
        reddit_image_url_from_post(
            {
                "gallery_data": {"items": [{"media_id": "second"}, {"media_id": "first"}]},
                "media_metadata": {
                    "first": {"s": {"u": "https://preview.redd.it/first.jpg?width=960&amp;format=pjpg"}},
                    "second": {"s": {"u": "https://preview.redd.it/second.jpg?width=960&amp;format=pjpg"}},
                },
            }
        )
        == "https://preview.redd.it/second.jpg?width=960&format=pjpg"
    )


def test_reddit_image_url_from_preview_source():
    assert (
        reddit_image_url_from_post(
            {
                "preview": {
                    "images": [
                        {"source": {"url": "https://preview.redd.it/example.png?width=960&amp;format=png"}}
                    ]
                }
            }
        )
        == "https://preview.redd.it/example.png?width=960&format=png"
    )


def test_user_facing_download_error_cookie_and_private_cases():
    assert "expired" in user_facing_download_error("ERROR: cookies expired, login required").lower()
    assert "private" in user_facing_download_error("ERROR: this post is private").lower()


def test_user_facing_upload_error_large_file():
    assert "too large" in user_facing_upload_error(Exception("413 Request Entity Too Large")).lower()


def test_process_url_reddit_unsupported_direct_gif_sends_repost(monkeypatch):
    url = "https://old.reddit.com/r/Transmogrification/comments/1nwzkfx/flameforged_gnomie"
    sent = []

    async def fake_run_subprocess(cmd):
        return 1, (
            "WARNING: [generic] Falling back on generic information extractor\n"
            "ERROR: Unsupported URL: "
            "https://www.reddit.com/media?url=https%3A%2F%2Fi.redd.it%2Fisd965xyhwsf1.gif"
        )

    async def fail_success(filepath):
        raise AssertionError("on_success should not run")

    async def fail_error(message):
        raise AssertionError(f"on_error should not run: {message}")

    async def fail_too_big(duration):
        raise AssertionError(f"on_too_big should not run: {duration}")

    async def on_no_video(log_text=""):
        gif_url = reddit_gif_url_from_log(log_text)
        if gif_url:
            sent.append(("send", gif_url))

    monkeypatch.setattr("bot.run_subprocess", fake_run_subprocess)
    monkeypatch.setattr("bot.reddit_has_video", lambda url: asyncio.sleep(0, result=True))

    asyncio.run(process_url(url, None, fail_success, fail_error, fail_too_big, on_no_video))

    assert sent == [("send", "https://i.redd.it/isd965xyhwsf1.gif")]


def test_process_url_reddit_no_video_direct_image_sends_repost(monkeypatch):
    url = "https://www.reddit.com/r/shitposting/comments/example/title/"
    sent = []

    async def fail_success(filepath):
        raise AssertionError("on_success should not run")

    async def fail_error(message):
        raise AssertionError(f"on_error should not run: {message}")

    async def fail_too_big(duration):
        raise AssertionError(f"on_too_big should not run: {duration}")

    async def on_no_video(log_text=""):
        image_url = reddit_image_url_from_log(log_text)
        if image_url:
            sent.append(("send_image", image_url))

    monkeypatch.setattr("bot.reddit_has_video", lambda url: asyncio.sleep(0, result=False))
    monkeypatch.setattr("bot.reddit_image_url", lambda url: asyncio.sleep(0, result="https://i.redd.it/example.jpg"))

    asyncio.run(process_url(url, None, fail_success, fail_error, fail_too_big, on_no_video))

    assert sent == [("send_image", "https://i.redd.it/example.jpg")]


def test_reddit_gif_repost_deletes_after_success():
    events = []
    message = FakeMessage(events)

    result = asyncio.run(send_reddit_gif_repost(message, "https://i.redd.it/isd965xyhwsf1.gif"))

    assert result is True
    assert events == [
        ("send", "https://i.redd.it/isd965xyhwsf1.gif"),
        ("delete", None),
    ]


def test_reddit_gif_repost_requires_embed_links_permission():
    events = []
    guild = FakeGuild(FakePermissions(embed_links=False))
    message = FakeMessage(events, guild=guild)

    result = asyncio.run(send_reddit_gif_repost(message, "https://i.redd.it/isd965xyhwsf1.gif"))

    assert result is False
    assert events == []


def test_reddit_image_repost_uploads_file_and_deletes_after_success(monkeypatch, tmp_path):
    image_path = tmp_path / "example.jpg"
    image_path.write_bytes(b"image")
    events = []
    message = FakeMessage(events)

    monkeypatch.setattr(
        "bot.download_reddit_image",
        lambda url, guild: asyncio.sleep(0, result=str(image_path)),
    )

    result = asyncio.run(send_reddit_image_repost(message, "https://i.redd.it/example.jpg"))

    assert result is True
    assert events == [
        ("send_file", "example.jpg"),
        ("delete", None),
    ]


def test_successful_instagram_image_rewrite_deletes_original_message():
    events = []
    message = FakeMessage(events)
    log_text = f"{INSTAGRAM_IMAGE_MARKER}\n[NOVIDEO]"

    result = asyncio.run(
        send_instagram_image_rewrite(
            message,
            "https://www.instagram.com/p/DWw6liflLcV/",
            log_text,
        )
    )

    assert result is True
    assert events == [
        ("send", "https://www.kkinstagram.com/p/DWw6liflLcV/"),
        ("delete", None),
    ]


def test_failed_instagram_image_rewrite_repost_does_not_delete(monkeypatch):
    monkeypatch.setattr("bot.discord.HTTPException", FakeDiscordError)
    events = []
    message = FakeMessage(events, send_error=FakeDiscordError("send failed"))
    log_text = f"{INSTAGRAM_IMAGE_MARKER}\n[NOVIDEO]"

    result = asyncio.run(
        send_instagram_image_rewrite(
            message,
            "https://www.instagram.com/p/DWw6liflLcV/",
            log_text,
        )
    )

    assert result is False
    assert events == [("send", "https://www.kkinstagram.com/p/DWw6liflLcV/")]


def test_instagram_video_download_path_does_not_delete_original_message():
    events = []
    message = FakeMessage(events)

    result = asyncio.run(
        send_instagram_image_rewrite(
            message,
            "https://www.instagram.com/p/DYPONveG9kZ/",
            "[INFO] Downloaded: 1.0 MB",
        )
    )

    assert result is False
    assert events == []


def test_instagram_image_rewrite_forbidden_delete_failure_does_not_crash(monkeypatch):
    monkeypatch.setattr("bot.discord.Forbidden", FakeDiscordError)
    events = []
    message = FakeMessage(events, delete_error=FakeDiscordError("delete forbidden"))
    log_text = f"{INSTAGRAM_IMAGE_MARKER}\n[NOVIDEO]"

    result = asyncio.run(
        send_instagram_image_rewrite(
            message,
            "https://www.instagram.com/p/DWw6liflLcV/",
            log_text,
        )
    )

    assert result is True
    assert events == [
        ("send", "https://www.kkinstagram.com/p/DWw6liflLcV/"),
        ("delete", None),
    ]


def test_process_url_instagram_image_text_only_no_video_sends_rewritten_url(monkeypatch):
    url = "https://www.instagram.com/p/DWw6liflLcV/"
    sent = []

    async def fake_run_subprocess(cmd):
        assert "--dump-single-json" in cmd
        assert "--no-playlist" not in cmd
        assert "--ignore-no-formats" in cmd
        return 0, (
            '{"id": "DWw6liflLcV", "title": "Video by memedwyd", '
            '"description": "text post", "formats": [], "_type": "video"}'
        )

    async def fail_success(filepath):
        raise AssertionError("on_success should not run")

    async def fail_error(message):
        raise AssertionError(f"on_error should not run: {message}")

    async def fail_too_big(duration):
        raise AssertionError(f"on_too_big should not run: {duration}")

    async def on_no_video(log_text=""):
        rewritten = rewrite_instagram_image_url(url, log_text)
        if rewritten:
            sent.append(("send", rewritten))

    monkeypatch.setattr("bot.run_subprocess", fake_run_subprocess)

    asyncio.run(process_url(url, None, fail_success, fail_error, fail_too_big, on_no_video))

    assert sent == [("send", "https://www.kkinstagram.com/p/DWw6liflLcV/")]


@pytest.mark.parametrize(
    "output",
    [
        "ERROR: This account is private",
        "ERROR: The requested content is not available, it may have been deleted",
        "ERROR: Login required",
        "ERROR: HTTP Error 429: Too Many Requests",
        "ERROR: Unable to download webpage: timed out",
    ],
)
def test_process_url_instagram_private_deleted_login_rate_limit_timeout_sends_no_rewrite(
    monkeypatch, output
):
    url = "https://www.instagram.com/p/DYPONveG9kZ"
    sent = []
    calls = []
    errors = []

    async def fake_run_subprocess(cmd):
        calls.append(cmd)
        return 1, output

    async def fail_success(filepath):
        raise AssertionError("on_success should not run")

    async def on_error(message):
        errors.append(message)

    async def fail_too_big(duration):
        raise AssertionError(f"on_too_big should not run: {duration}")

    async def on_no_video(log_text=""):
        rewritten = rewrite_instagram_image_url(url, log_text)
        if rewritten:
            sent.append(("send", rewritten))

    monkeypatch.setattr("bot.run_subprocess", fake_run_subprocess)

    asyncio.run(process_url(url, None, fail_success, on_error, fail_too_big, on_no_video))

    assert calls
    assert sent == []


def test_process_url_instagram_video_success_sends_no_rewrite(monkeypatch):
    url = "https://www.instagram.com/p/DYPONveG9kZ"
    sent = []
    succeeded = []

    async def fake_run_subprocess(cmd):
        if "--dump-single-json" in cmd:
            assert "--no-playlist" not in cmd
            assert "--ignore-no-formats" in cmd
            return 0, (
                '{"entries": ['
                '{"duration": null, "ext": "jpg"}, null, '
                '{"duration": null, "ext": "jpg"}, null, '
                '{"duration": 12.3, "ext": "mp4"}]}'
            )

        assert "--playlist-items" in cmd
        assert cmd[cmd.index("--playlist-items") + 1] == "5"
        assert "--no-playlist" not in cmd
        output_path = Path(cmd[cmd.index("-o") + 1].replace("%(title)s.%(ext)s", "clip.mp4"))
        output_path.write_bytes(b"video")
        return 0, "downloaded"

    async def on_success(filepath):
        succeeded.append(Path(filepath).name)

    async def fail_error(message):
        raise AssertionError(f"on_error should not run: {message}")

    async def fail_too_big(duration):
        raise AssertionError(f"on_too_big should not run: {duration}")

    async def on_no_video(log_text=""):
        rewritten = rewrite_instagram_image_url(url, log_text)
        if rewritten:
            sent.append(("send", rewritten))

    async def fake_compress_to_target(src, dest, target_mb, duration=None):
        Path(dest).write_bytes(b"video")
        return True, "0.01 MB"

    monkeypatch.setattr("bot.run_subprocess", fake_run_subprocess)
    monkeypatch.setattr("bot.get_duration", lambda filepath: asyncio.sleep(0, result=12.3))
    monkeypatch.setattr(
        "bot.get_media_info",
        lambda filepath: asyncio.sleep(0, result={"format": {"duration": "12.3"}, "streams": []}),
    )
    monkeypatch.setattr("bot.compress_to_target", fake_compress_to_target)
    monkeypatch.setattr("bot.cleanup_tmp", lambda filepath: asyncio.sleep(0))

    asyncio.run(process_url(url, None, on_success, fail_error, fail_too_big, on_no_video))

    assert succeeded == ["compressed.mp4"]
    assert sent == []
