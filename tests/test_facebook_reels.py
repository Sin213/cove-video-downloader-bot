import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import bot


REEL = "https://www.facebook.com/reel/4010805205882836"
TWITTER = "https://twitter.com/example/status/123"


def make_message(content, *, bot_author=False, guild=None):
    permissions = SimpleNamespace(send_messages=True, attach_files=True, add_reactions=True)
    return SimpleNamespace(
        content=content, id=67890, guild=guild, reference=None, mentions=[],
        author=SimpleNamespace(bot=bot_author, id=12345, display_name="Tester"),
        channel=SimpleNamespace(send=AsyncMock(), permissions_for=Mock(return_value=permissions)),
        add_reaction=AsyncMock(), remove_reaction=AsyncMock(), reply=AsyncMock(), delete=AsyncMock(),
    )


@pytest.fixture
def dispatch(monkeypatch):
    process = AsyncMock()
    tasks = []

    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(bot, "process_url", process)
    monkeypatch.setattr(bot, "spawn_tracked", spawn)

    def run(message, *, repeats=1):
        async def drive():
            for _ in range(repeats):
                await bot.client.on_message(message)
            await asyncio.gather(*tasks)

        asyncio.run(drive())
        return process

    return run


VALID_REELS = [
    REEL,
    "https://facebook.com/reel/1",
    REEL + "/",
    REEL + "?s=123&mibextid=abc",
    REEL + "#ignored",
    "https://WWW.FACEBOOK.COM/reel/123",
    "http://facebook.com/reel/123",
    "http://facebook.com:80/reel/123",
    "https://facebook.com:443/reel/123",
    "https://facebook.com/reel/" + "9" * 32,
]


@pytest.mark.parametrize("url", VALID_REELS)
@pytest.mark.parametrize("in_guild", [False, True])
def test_reel_message_dispatches_exact_url_once(dispatch, url, in_guild):
    guild = SimpleNamespace(id=1, me=object()) if in_guild else None
    message = make_message(url, guild=guild)
    assert bot.extract_supported_url(message.content) == url
    process = dispatch(message)
    process.assert_awaited_once()
    assert process.await_args.args[:2] == (url, guild)


def test_reel_in_text_strips_trailing_punctuation(dispatch):
    message = make_message(f"Look at this ({REEL}).")
    assert bot.extract_supported_url(message.content) == REEL
    process = dispatch(message)
    process.assert_awaited_once()
    assert process.await_args.args[0] == REEL


@pytest.mark.parametrize("content, expected", [
    pytest.param("<https://www.facebook.com/reel/123>", "https://www.facebook.com/reel/123", id="angle-link"),
    pytest.param("[watch](https://www.facebook.com/reel/123)", "https://www.facebook.com/reel/123", id="markdown-link"),
    pytest.param("https://facebook.com/posts/1\nhttps://facebook.com/reel/123", "https://facebook.com/reel/123", id="newline-links"),
    pytest.param(r"https://facebook.com\reel\123", None, id="backslashes"),
    pytest.param("https://facebook.com./reel/123", None, id="trailing-dot-host"),
    pytest.param("https://facebook%2Ecom/reel/123", None, id="encoded-host"),
    pytest.param("https://facebook.com/%72eel/123", None, id="encoded-path"),
    pytest.param("https://facebook.com:/reel/123", "https://facebook.com:/reel/123", id="empty-port"),
    pytest.param("https://facebook.com/reel/12\x003", None, id="embedded-nul"),
    pytest.param("https://facebook.com/reel/12\t3", "https://facebook.com/reel/12", id="embedded-tab"),
])
def test_markdown_and_odd_inputs_follow_existing_extraction(dispatch, content, expected):
    assert bot.extract_supported_url(content) == expected
    message = make_message(content)
    process = dispatch(message)
    if expected is None:
        process.assert_not_called()
        message.add_reaction.assert_not_awaited()
    else:
        process.assert_awaited_once()
        assert process.await_args.args[:2] == (expected, None)


INVALID_REELS = [
    "https://facebook.com/posts/123",
    "https://facebook.com/watch?v=123",
    "https://facebook.com/videos/123",
    "https://facebook.com/profile.php?id=123",
    "https://facebook.com/someuser",
    "https://facebook.com/share/r/123",
    "https://facebook.com/",
    "https://facebook.com/reel",
    "https://facebook.com/reel/",
    "https://facebook.com/reel/abc",
    "https://facebook.com/reel/１２３",
    "https://facebook.com/reel/١٢٣",
    "https://facebook.com/reel/" + "1" * 33,
    "https://facebook.com/reel/123/extra",
    "https://facebook.com/reel/123//",
    "https://facebook.com/Reel/123",
    "https://facebook.com/reel/+123",
    "https://facebook.com/reel/%31",
    "https://facebook.com/reel/123;extra",
    "https://facebook.com/reel/123;?x=1",
    "https://facebook.com/reel/123;#f",
    "https://facebook.com/reel/123/;?x=1",
    "https://facebook.com/reel/123;a=b",
    "https://facebook.com/reel/123/;a=b",
    "https://facebook.com/reel/12;3",
    "https://facebook.com/reel/123;;?x=1",
    "https://facebook.com/reel/123/;;#f",
    "https://facebook.com.attacker.invalid/reel/123",
    "https://notfacebook.com/reel/123",
    "https://facebook.com@evil.invalid/reel/123",
    "https://evil.invalid@facebook.com/reel/1",
    "https://user:password@www.facebook.com/reel/123",
    "https://@facebook.com/reel/123",
    "https://fb.watch/123",
    "https://m.facebook.com/reel/123",
    "https://web.facebook.com/reel/123",
    "https://other.facebook.com/reel/123",
    "https://www.www.facebook.com/reel/123",
    "https://facebook.com:444/reel/123",
    "https://facebook.com:80/reel/123",
    "http://facebook.com:443/reel/123",
    "https://facebook.com:invalid/reel/123",
    "https://facebook.com:99999/reel/123",
    "ftp://facebook.com/reel/123",
    "javascript://facebook.com/reel/123",
    "//facebook.com/reel/123",
]


@pytest.mark.parametrize("url", INVALID_REELS)
def test_non_reel_or_untrusted_url_never_dispatches(dispatch, url):
    assert bot.extract_supported_url(url) is None
    process = dispatch(make_message(url))
    process.assert_not_called()


@pytest.mark.parametrize("url, extracted", [
    pytest.param("https://facebook.com/reel/123;", "https://facebook.com/reel/123", id="one-semicolon"),
    pytest.param("https://facebook.com/reel/123/;", "https://facebook.com/reel/123/", id="slash-semicolon"),
    pytest.param("https://facebook.com/reel/123;;", "https://facebook.com/reel/123", id="two-semicolons"),
])
def test_terminal_semicolons_reject_raw_path_but_trim_message_punctuation(dispatch, url, extracted):
    assert bot.is_facebook_reel_url(url) is False
    assert bot.extract_supported_url(url) == extracted
    process = dispatch(make_message(url))
    process.assert_awaited_once()
    assert process.await_args.args[:2] == (extracted, None)


@pytest.mark.parametrize("url, extracted", [
    ("https://facebook.com/reel/123?next=a;b", "https://facebook.com/reel/123?next=a;b"),
    ("https://facebook.com/reel/123/?x=1;", "https://facebook.com/reel/123/?x=1"),
])
def test_query_semicolons_preserve_valid_reel_path(dispatch, url, extracted):
    assert bot.is_facebook_reel_url(url)
    assert bot.extract_supported_url(url) == extracted
    process = dispatch(make_message(url))
    process.assert_awaited_once()
    assert process.await_args.args[:2] == (extracted, None)


@pytest.mark.parametrize("content, expected", [
    (f"https://facebook.com/posts/123 {REEL}", REEL),
    (f"{REEL} {TWITTER}", REEL),
    (f"https://facebook.com/watch?v=123 {TWITTER}", TWITTER),
])
def test_multiple_links_keep_first_supported_url(dispatch, content, expected):
    assert bot.extract_supported_url(content) == expected
    process = dispatch(make_message(content))
    process.assert_awaited_once()
    assert process.await_args.args[0] == expected


def test_bot_authored_reel_ignored(dispatch):
    process = dispatch(make_message(REEL, bot_author=True))
    process.assert_not_called()


def test_duplicate_message_dispatches_once(dispatch):
    process = dispatch(make_message(REEL), repeats=2)
    process.assert_awaited_once()
    assert process.await_args.args[0] == REEL


@pytest.mark.parametrize("url, expected", [
    (TWITTER, TWITTER),
    ("https://instagram.com/reel/abc/", "https://instagram.com/reel/abc/"),
    ("https://instagram.com/p/abc/", "https://instagram.com/p/abc/"),
    ("https://instagram.com/profile/", None),
    ("https://oginstagram.com/p/abc/", None),
    ("https://reddit.com/r/test/comments/123/title/", "https://reddit.com/r/test/comments/123/title/"),
    ("https://i.redd.it/example.gif", None),
    ("https://twitter.com/example/status/123/photo/1", None),
])
def test_existing_auto_detection_unchanged(url, expected):
    assert bot.extract_supported_url(url) == expected


def test_facebook_is_not_a_broad_auto_domain_and_manual_syntax_still_accepts_reels():
    assert "facebook.com" not in bot.AUTO_DOWNLOAD_DOMAINS
    assert bot._validate_manual_url_syntax(REEL) == (True, "")


@pytest.mark.parametrize("url, expected", [
    ("HTTPS://WWW.FACEBOOK.COM/reel/123", True),
    ("https://facebook.com/reel/123?next=/extra#ignored", True),
    ("https://[invalid/reel/123", False),
    ("ftp://facebook.com/reel/123", False),
])
def test_predicate_uses_parsed_url_semantics(url, expected):
    assert callable(getattr(bot, "is_facebook_reel_url", None))
    assert bot.is_facebook_reel_url(url) is expected


def test_error_callback_replies_without_deleting_source(dispatch, monkeypatch):
    async def fail(url, guild, on_success, on_error, *args):
        await on_error("Download failed.")

    monkeypatch.setattr(bot, "process_url", AsyncMock(side_effect=fail))
    message = make_message(REEL)
    dispatch(message)
    message.reply.assert_awaited_once_with("❌ Download failed.", mention_author=False)
    message.channel.send.assert_not_awaited()
    message.delete.assert_not_awaited()
    message.remove_reaction.assert_awaited_once_with("⏳", bot.client.user)


@pytest.mark.parametrize("log_text", ["[NOVIDEO]", "[INSTAGRAM_LINK]\n[NOVIDEO]", "[INSTAGRAM_IMAGE]\n[NOVIDEO]"])
def test_no_video_callback_does_not_rewrite_or_delete(dispatch, monkeypatch, log_text):
    async def no_video(url, guild, on_success, on_error, on_too_big, on_no_video):
        await on_no_video(log_text)

    monkeypatch.setattr(bot, "process_url", AsyncMock(side_effect=no_video))
    message = make_message(REEL)
    dispatch(message)
    message.channel.send.assert_not_awaited()
    message.reply.assert_not_awaited()
    message.delete.assert_not_awaited()
    message.remove_reaction.assert_awaited_once_with("⏳", bot.client.user)


@pytest.mark.parametrize("context, deletes_source", [("ordinary", False), ("whitelist", True), ("friend", True)])
def test_success_callback_preserves_existing_deletion_rules(dispatch, monkeypatch, context, deletes_source):
    async def success(url, guild, on_success, *args):
        await on_success("/tmp/offline-reel.mp4")

    monkeypatch.setattr(bot, "process_url", AsyncMock(side_effect=success))
    monkeypatch.setattr(bot, "WHITELIST_IDS", {12345} if context == "whitelist" else set())
    monkeypatch.setattr(bot, "EFFECTIVE_FRIEND_GUILD_IDS", {99})
    sent = SimpleNamespace(id=54321, add_reaction=AsyncMock())
    send = AsyncMock(return_value=sent)
    monkeypatch.setattr(bot, "send_file_with_retry", send)
    guild = SimpleNamespace(id=99, me=object()) if context == "friend" else None
    message = make_message(REEL, guild=guild)
    dispatch(message)
    send.assert_awaited_once()
    assert send.await_args.args == (message.channel.send, "/tmp/offline-reel.mp4")
    assert message.delete.await_count == int(deletes_source)
    message.reply.assert_not_awaited()


@pytest.mark.parametrize("context", ["ordinary", "whitelist", "friend"])
def test_source_deletion_waits_for_upload_completion(dispatch, monkeypatch, context):
    order = []
    upload_started, release_upload = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(bot, "WHITELIST_IDS", {12345} if context == "whitelist" else set())
    monkeypatch.setattr(bot, "EFFECTIVE_FRIEND_GUILD_IDS", {99})
    guild = SimpleNamespace(id=99, me=object()) if context == "friend" else None
    message = make_message(REEL, guild=guild)
    message.delete.side_effect = lambda: order.append("source-delete")
    sent = SimpleNamespace(id=54321, add_reaction=AsyncMock())

    async def upload(*args, **kwargs):
        order.append("upload-start")
        upload_started.set()
        await release_upload.wait()
        order.append("upload-complete")
        return sent

    async def success(url, guild, on_success, *args):
        task = asyncio.create_task(on_success("/tmp/offline-reel.mp4"))
        try:
            await asyncio.wait_for(upload_started.wait(), timeout=1)
            assert not task.done()
            message.delete.assert_not_awaited()
            assert order == ["upload-start"]
            assert not bot._deletable and not bot._friend_posts
        finally:
            release_upload.set()
            await task

    process = AsyncMock(side_effect=success)
    send = AsyncMock(side_effect=upload)
    monkeypatch.setattr(bot, "process_url", process)
    monkeypatch.setattr(bot, "send_file_with_retry", send)
    dispatch(message)
    process.assert_awaited_once()
    send.assert_awaited_once()
    expected = ["upload-start", "upload-complete"]
    if context != "ordinary":
        expected.append("source-delete")
        message.delete.assert_awaited_once()
    else:
        message.delete.assert_not_awaited()
    assert order == expected


@pytest.mark.parametrize("context", ["ordinary", "whitelist", "friend"])
def test_upload_failure_keeps_source_and_registers_no_success_post(dispatch, monkeypatch, context):
    async def success(url, guild, on_success, *args):
        await on_success("/tmp/offline-reel.mp4")

    process = AsyncMock(side_effect=success)
    send = AsyncMock(side_effect=RuntimeError("offline upload failed"))
    monkeypatch.setattr(bot, "process_url", process)
    monkeypatch.setattr(bot, "send_file_with_retry", send)
    monkeypatch.setattr(bot, "WHITELIST_IDS", {12345} if context == "whitelist" else set())
    monkeypatch.setattr(bot, "EFFECTIVE_FRIEND_GUILD_IDS", {99})
    guild = SimpleNamespace(id=99, me=object()) if context == "friend" else None
    message = make_message(REEL, guild=guild)

    # The stub directly invokes on_success; unlike real process_url, it lets
    # upload errors propagate, so explicitly require that task exception.
    with pytest.raises(RuntimeError, match="offline upload failed"):
        dispatch(message)

    process.assert_awaited_once()
    send.assert_awaited_once()
    assert send.await_args.args == (message.channel.send, "/tmp/offline-reel.mp4")
    message.remove_reaction.assert_awaited_once_with("⏳", bot.client.user)
    message.delete.assert_not_awaited()
    message.channel.send.assert_not_awaited()
    message.reply.assert_not_awaited()
    assert not bot._deletable and not bot._friend_posts
