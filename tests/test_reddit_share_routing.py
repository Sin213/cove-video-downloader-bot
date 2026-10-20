import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot


SHARE = "https://www.reddit.com/r/test/s/AbC123"
TARGET = "https://www.reddit.com/r/test/s/AbC123"
LOCATION = "/r/test/comments/abc/title/"
RESOLVED = "https://www.reddit.com" + LOCATION
DECOY = "https://x.com/u/status/1?u=reddit.com/r/x/s/y"

VALID_SHARES = [
    (SHARE, ""),
    ("https://reddit.com/r/test/s/AbC123", ""),
    ("http://reddit.com/r/test/s/AbC123?utm_source=share", "?utm_source=share"),
    ("https://old.reddit.com/r/test/s/AbC123", ""),
    ("https://np.reddit.com/r/test/s/AbC123", ""),
    ("https://m.reddit.com/r/test/s/AbC123", ""),
    (SHARE + "/", "/"),
    (SHARE + "/extra", "/extra"),
    (SHARE + "?utm_source=share#ignored", "?utm_source=share"),
    (SHARE + ";key=value?next=x#ignored", ";key=value?next=x"),
]

OTHER_URLS = [
    "https://x.com/u/status/1",
    "https://twitter.com/u/status/1",
    "https://www.tiktok.com/@a/video/1",
    "https://instagram.com/p/abc/",
    "https://facebook.com/posts/1",
    "https://facebook.com/reel/123",
]
DECOY_SUFFIXES = [
    "?u=reddit.com/r/x/s/y",
    "?u=https://www.reddit.com/r/x/s/y",
    "#reddit.com/r/x/s/y",
    "#https://www.reddit.com/r/x/s/y",
    "/reddit.com/r/x/s/y",
    "/https://www.reddit.com/r/x/s/y",
    "?u=https%3A%2F%2Freddit.com%2Fr%2Fx%2Fs%2Fy",
    "?next=https://example.com/?u=https://www.reddit.com/r/x/s/y",
]
ORDINARY_REDDIT_URLS = [
    RESOLVED,
    "https://redd.it/abc",
    "https://www.redd.it/abc",
]
INVALID_SHARES = [base + suffix for base in OTHER_URLS for suffix in DECOY_SUFFIXES] + [
    f"https://{host}/r/x/s/y"
    for host in ("notreddit.com", "reddit.com.evil.com", "evil-reddit.com", "redditXcom", "reddit.com.")
] + [
    "https://reddit.com@evil.com/r/x/s/y",
    "https://user:pw@reddit.com/r/x/s/y",
    "https://@reddit.com/r/x/s/y",
    "http://reddit.com:80/r/test/s/AbC123",
    "https://reddit.com:443/r/test/s/AbC123",
    "https://reddit.com:/r/x/s/y",
    "https://reddit.com:8443/r/x/s/y",
    "https://reddit.com:80/r/x/s/y",
    "http://reddit.com:443/r/x/s/y",
    "https://reddit.com:invalid/r/x/s/y",
    "https://reddit.com:99999/r/x/s/y",
    "ftp://reddit.com/r/x/s/y",
    "javascript://reddit.com/r/x/s/y",
    "javascript:https://reddit.com/r/x/s/y",
    "//reddit.com/r/x/s/y",
    "reddit.com/r/x/s/y",
    "https://[::1/r/x/s/y",
    "https://[v1.reddit.com]/r/x/s/y",
    "https://[::1]/r/x/s/y",
    "https://reddit.com/r/x/s/y\n",
    " https://reddit.com/r/x/s/y",
    "https://reddit.com/r/x/s/y ",
    "https://evil.example\\.reddit.com/r/x/s/y",
    "https://reddit.com\\@evil.com/r/x/s/y",
    "https://reddit.com\\.evil.com/r/x/s/y",
    "https://r\u0131ddit.com/r/x/s/y",
    "https://redd\u0131t.com/r/x/s/y",
    "https://\u212a.reddit.com/r/x/s/y",
    "https://reddit.com%2eevil.com/r/x/s/y",
    "https://%72eddit.com/r/x/s/y",
    "https:reddit.com/r/x/s/y",
    "https:/reddit.com/r/x/s/y",
    RESOLVED + "?u=reddit.com/r/x/s/y",
    "https://reddit.com/?next=/r/x/s/y",
    "https://reddit.com/#/r/x/s/y",
    "https://reddit.com/;next=reddit.com/r/x/s/y",
] + [
    f"https://red{control}dit.com/r/x/s/y" for control in ("\t", "\r", "\n")
] + [
    f"https://reddit.com/r/x{control}/s/y" for control in ("\t", "\r", "\n")
] + [
    "https://reddit.com" + path
    for path in (
        "/r/x/comments/1/t", "/r/x/s", "/r/x/s/", "/r//s/y",
        "/x/r/x/s/y", "/u/foo/s/y", "/r/x/y/s/z", "/r%2Fx/s/y",
    )
]


@pytest.fixture
def http_boundary(monkeypatch):
    session = SimpleNamespace(calls=[], status=302, headers={"Location": LOCATION}, error=None)

    @asynccontextmanager
    async def response():
        yield SimpleNamespace(status=session.status, headers=session.headers)

    def get(url, **kwargs):
        session.calls.append((url, kwargs))
        if session.error is not None:
            raise session.error
        return response()

    session.get = get
    session.persist = AsyncMock()
    monkeypatch.setattr(bot, "_get_http_session", lambda: session)
    monkeypatch.setattr(bot, "_persist_cache_entry_async", session.persist)
    return session


@pytest.mark.parametrize("url, suffix", VALID_SHARES)
def test_share_resolves_with_existing_request_and_cache(http_boundary, url, suffix):
    assert asyncio.run(bot.resolve_reddit_shortlink(url)) == RESOLVED
    assert http_boundary.calls == [(TARGET + suffix, {
        "headers": {
            "User-Agent": bot.REDDIT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
        "allow_redirects": False,
        "timeout": bot.aiohttp.ClientTimeout(total=8),
    })]
    http_boundary.persist.assert_awaited_once_with(url, RESOLVED, "shortlink", bot.SHORTLINK_CACHE_TTL)
    http_boundary.calls.clear()
    assert asyncio.run(bot.resolve_reddit_shortlink(url)) == RESOLVED
    assert http_boundary.calls == []
    http_boundary.persist.assert_awaited_once()


@pytest.mark.parametrize("failure", ["get-raises", "non-redirect"])
def test_share_request_failure_returns_original(http_boundary, failure):
    if failure == "get-raises":
        http_boundary.error = RuntimeError("offline request failed")
    else:
        http_boundary.status = 200
    assert asyncio.run(bot.resolve_reddit_shortlink(SHARE)) == SHARE
    assert len(http_boundary.calls) == 1
    http_boundary.persist.assert_not_awaited()


def test_uppercase_host_is_normalized_not_preserved_behavior(http_boundary):
    # Deliberate normalization: the baseline regex did not accept uppercase hosts.
    url = "HTTPS://WWW.REDDIT.COM/r/test/s/AbC123"
    assert bot.is_reddit_share_url(url) is True
    assert asyncio.run(bot.resolve_reddit_shortlink(url)) == RESOLVED
    assert len(http_boundary.calls) == 1
    assert http_boundary.calls[0][0] == TARGET


@pytest.mark.parametrize("url", INVALID_SHARES)
def test_non_share_never_requests_reddit(http_boundary, url):
    result = asyncio.run(bot.resolve_reddit_shortlink(url))
    assert http_boundary.calls == []
    assert result == url
    http_boundary.persist.assert_not_awaited()


@pytest.mark.parametrize("url", ORDINARY_REDDIT_URLS)
def test_ordinary_reddit_url_passes_through(http_boundary, url):
    assert asyncio.run(bot.resolve_reddit_shortlink(url)) == url
    assert http_boundary.calls == []


@pytest.mark.parametrize("content, selected", [
    (f"Look at this {DECOY}", DECOY),
    (f"{SHARE} {DECOY}", SHARE),
    (f"{DECOY} {SHARE}", DECOY),
])
def test_pipeline_preserves_first_supported_url(http_boundary, content, selected):
    assert bot.extract_supported_url(content) == selected
    assert http_boundary.calls == []


def test_pipeline_selected_share_resolves_once(http_boundary):
    selected = bot.extract_supported_url(f"{SHARE} {DECOY}")
    assert asyncio.run(bot.resolve_reddit_shortlink(selected)) == RESOLVED
    assert len(http_boundary.calls) == 1


@pytest.mark.parametrize("content", [f"Look at this {DECOY}", f"{DECOY} {SHARE}"])
def test_non_share_pipeline_never_requests_reddit(http_boundary, content):
    selected = bot.extract_supported_url(content)
    assert selected == DECOY
    result = asyncio.run(bot.resolve_reddit_shortlink(selected))
    assert http_boundary.calls == []
    assert result == selected


@pytest.mark.parametrize("url, expected", [
    (url, True) for url, _ in VALID_SHARES
] + [
    (url, False) for url in INVALID_SHARES + ORDINARY_REDDIT_URLS
])
def test_share_predicate_uses_parsed_url(url, expected):
    assert bot.is_reddit_share_url(url) is expected
