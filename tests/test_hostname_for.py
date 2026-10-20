import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import bot


MALFORMED_URLS = [
    f"{scheme}://{authority}"
    for scheme in ("https", "http")
    for authority in ("[invalid/", "invalid]/x", "a[b/", "[::1/", "[]/")
]
TIKTOK = "https://www.tiktok.com/@a/video/1"


@pytest.mark.parametrize("url", MALFORMED_URLS)
def test_hostname_for_malformed_authority(url):
    assert bot.hostname_for(url) == ""


# Expected values measured on the unchanged bot before adding the guards.
@pytest.mark.parametrize("url, expected", [
    ("https://example.com/path", "example.com"),
    ("https://EXAMPLE.COM/path", "example.com"),
    ("https://www.example.com/path", "example.com"),
    ("https://www.www.example.com/path", "www.example.com"),
    ("https://user:pass@example.com/path", "example.com"),
    ("https://example.com:8443/path", "example.com"),
    ("https://example.com:443/path", "example.com"),
    ("https://example.com:bad/path", "example.com"),
    ("https://[::1]/path", "::1"),
    ("https://[v1.x]/path", "v1.x"),
    ("https://bücher.example/path", "bücher.example"),
    ("https://xn--bcher-kva.example/path", "xn--bcher-kva.example"),
    ("", ""),
    ("no-host", ""),
    ("http://", ""),
    ("//WWW.Example.COM/path", "example.com"),
    (r"https://example.com\\path", r"example.com\\path"),
    ("https://exa\tmple.com/path", "example.com"),
    ("https://example%2Ecom/path?x=1#fragment", "example%2ecom"),
    ("https://example.com.evil/path", "example.com.evil"),
])
def test_hostname_for_parseable_input_unchanged(url, expected):
    assert bot.hostname_for(url) == expected


@pytest.fixture
def forbid_external_work(monkeypatch):
    guards = []
    for target, name in (
        (bot, "resolve_reddit_shortlink"),
        (bot, "_validate_manual_url_dns"),
        (bot, "process_url"),
        (bot, "download_and_compress"),
        (bot, "spawn_tracked"),
        (socket, "getaddrinfo"),
        (socket, "create_connection"),
        (bot.aiohttp, "ClientSession"),
    ):
        guard = Mock(side_effect=AssertionError(f"Unexpected call to {name}"))
        monkeypatch.setattr(target, name, guard)
        guards.append(guard)
    yield
    for guard in guards:
        guard.assert_not_called()


@pytest.mark.parametrize("url", MALFORMED_URLS)
def test_extract_skips_malformed_candidates(url, forbid_external_work):
    assert bot.extract_supported_url(url) is None
    assert bot.extract_supported_url(f"{url} {TIKTOK}") == TIKTOK
    assert bot.extract_supported_url(f"{TIKTOK} {url}") == TIKTOK
    assert bot.extract_supported_url(f"({url}).") is None
    assert bot.extract_supported_url(f"({url}). {TIKTOK}") == TIKTOK


def test_extract_skips_multiple_malformed_candidates(forbid_external_work):
    content = " ".join(MALFORMED_URLS)
    assert bot.extract_supported_url(content) is None
    assert bot.extract_supported_url(f"{content} {TIKTOK}") == TIKTOK


@pytest.mark.parametrize("url, expected", [
    (TIKTOK, TIKTOK),
    ("https://x.com/u/status/1", "https://x.com/u/status/1"),
    ("https://instagram.com/p/abc/", "https://instagram.com/p/abc/"),
    ("https://facebook.com/reel/123", "https://facebook.com/reel/123"),
    ("https://www.reddit.com/r/test/s/AbC123", "https://www.reddit.com/r/test/s/AbC123"),
    ("https://i.redd.it/example.gif", None),
    ("https://oginstagram.com/p/abc/", None),
    ("https://instagram.com/username/", None),
    ("https://fxtwitter.com/u/status/1", None),
])
def test_extract_parseable_input_unchanged(url, expected, forbid_external_work):
    assert bot.extract_supported_url(url) == expected


@pytest.mark.parametrize("in_guild", [False, True])
def test_on_message_malformed_is_inert(in_guild, forbid_external_work):
    message = SimpleNamespace(
        content=" ".join(MALFORMED_URLS),
        id=67890,
        guild=SimpleNamespace(id=1, me=object()) if in_guild else None,
        reference=None,
        author=SimpleNamespace(bot=False, id=12345, display_name="Tester"),
        channel=SimpleNamespace(send=AsyncMock()),
        delete=AsyncMock(),
        add_reaction=AsyncMock(),
    )
    asyncio.run(bot.client.on_message(message))
    message.delete.assert_not_called()
    message.add_reaction.assert_not_called()
    message.channel.send.assert_not_called()


def test_manual_malformed_url_syntax_unchanged():
    assert bot._validate_manual_url_syntax("https://[invalid/") == (False, "Invalid URL.")
