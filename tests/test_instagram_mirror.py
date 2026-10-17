import asyncio
from pathlib import Path

import pytest

import bot
from bot import (
    INSTAGRAM_IMAGE_MARKER,
    _instagram_mirror_media,
    fetch_instagram_gallery_image_urls,
    rewrite_instagram_image_url,
)

POST = "https://www.instagram.com/p/Ddgu6_9jH1V/?img_index=5"


def _fake_hosts(monkeypatch, verdicts):
    """verdicts: host -> (kind, url). Records the order hosts were asked."""
    asked = []

    async def fake_from_host(url, host):
        asked.append(host)
        return verdicts.get(host, ("error", None))

    monkeypatch.setattr("bot._instagram_mirror_media_from_host", fake_from_host)
    return asked


def test_mirror_chain_survives_dead_hosts_when_oginstagram_has_media(monkeypatch):
    # Mirrors as seen live on 2026-10-09: instagram7 NXDOMAIN, vxinstagram 401,
    # zzinstagram answers with no media tags, oginstagram serves the post.
    verdicts = {
        "instagram7.com": ("error", None),
        "vxinstagram.com": ("error", None),
        "zzinstagram.com": (None, None),
        "oginstagram.com": ("image", "https://oginstagram.com/offload/x/1"),
    }
    _fake_hosts(monkeypatch, verdicts)

    kind, media = asyncio.run(_instagram_mirror_media(POST))

    assert kind == "image"
    assert media == "https://oginstagram.com/offload/x/1"


def test_mirror_chain_asks_oginstagram_first_and_skips_dead_instagram7(monkeypatch):
    asked = _fake_hosts(monkeypatch, {"oginstagram.com": ("video", "https://v/x.mp4")})

    kind, _ = asyncio.run(_instagram_mirror_media(POST))

    assert kind == "video"
    assert asked == ["oginstagram.com"]
    assert "instagram7.com" not in bot.INSTAGRAM_MIRROR_HOSTS


def test_image_rewrite_uses_working_embed_host():
    result = rewrite_instagram_image_url(POST, f"{INSTAGRAM_IMAGE_MARKER}\n[NOVIDEO]")

    assert result == "https://oginstagram.com/p/Ddgu6_9jH1V/?img_index=5"


class _Resp:
    def __init__(self, status):
        self.status = status
        self.json_called = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        self.json_called = True
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def test_gallery_api_login_redirect_is_not_followed_into_html(monkeypatch):
    seen = {}
    resp = _Resp(302)

    class _Session:
        def get(self, url, **kwargs):
            seen.update(kwargs)
            return resp

    monkeypatch.setattr("bot._get_http_session", lambda: _Session())

    assert asyncio.run(fetch_instagram_gallery_image_urls(POST)) == []
    assert seen.get("allow_redirects") is False
    assert resp.json_called is False


@pytest.mark.parametrize("recovered", ["video", "image", None, "error"])
def test_cold_miss_recheck_is_bounded_and_cached(monkeypatch, recovered):
    asked, sleeps = [], []
    hosts = ("oginstagram.com", "zzinstagram.com")
    monkeypatch.setattr(bot, "INSTAGRAM_MIRROR_HOSTS", hosts)

    async def probe(url, host):
        asked.append(host)
        if host == hosts[1]:
            return "error", None
        if asked.count(host) == 2 and recovered:
            return recovered, "https://media.example/clip" if recovered != "error" else None
        return None, None

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(bot, "_instagram_mirror_media_from_host", probe)
    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    result = asyncio.run(bot._instagram_mirror_media(POST))
    expected = (recovered, "https://media.example/clip") if recovered in ("video", "image") else (None, None)
    assert result == expected
    assert asked == [hosts[0], hosts[1], hosts[0]]
    assert sleeps == [pytest.approx(1.5)]
    assert asyncio.run(bot._instagram_mirror_media(POST)) == expected
    assert len(asked) == 3 and len(sleeps) == 1


@pytest.mark.parametrize("kind", [None, "error", "image"])
def test_mirror_empty_or_error_pass_retry_policy(monkeypatch, kind):
    asked, sleeps = [], []

    async def probe(url, host):
        asked.append(host)
        return kind, "https://media.example/image" if kind == "image" else None

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(bot, "_instagram_mirror_media_from_host", probe)
    monkeypatch.setattr(bot.asyncio, "sleep", sleep)
    expected = (kind, "https://media.example/image" if kind == "image" else None)
    assert asyncio.run(bot._instagram_mirror_media(POST)) == expected
    assert asked == list(bot.INSTAGRAM_MIRROR_HOSTS) * (2 if kind is None else 1)
    assert sleeps == ([pytest.approx(1.5)] if kind is None else [])
    assert bool(bot._instagram_mirror_cache) is (kind != "error")
    if kind is None:
        assert asyncio.run(bot._instagram_mirror_media(POST)) == expected
        assert len(asked) == 2 * len(bot.INSTAGRAM_MIRROR_HOSTS) and len(sleeps) == 1


class _MediaResp(_Resp):
    def __init__(self, status=200, content_type="video/mp4", body=b"video"):
        super().__init__(status)
        self.content_type, self.body, self.content = content_type, body, self

    async def iter_chunked(self, size):
        yield self.body

    async def read(self, size):
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk


@pytest.mark.parametrize("failure", [403, 404, 500, "html", "empty", "exception", "cap", None])
def test_verified_mirror_video_fetch_result_and_credential_isolation(monkeypatch, tmp_path, failure):
    seen = []
    media = "https://media.example/offload/clip"
    metadata = _MediaResp(content_type="text/html", body=f'<meta property="og:video" content="{media}">'.encode())
    response = _MediaResp(
        status=failure if isinstance(failure, int) else 200,
        content_type="text/html" if failure == "html" else "video/mp4",
        body=b"" if failure == "empty" else b"video",
    )

    class Session:
        def get(self, url, **kwargs):
            seen.append((url, kwargs))
            if url == media and failure == "exception":
                raise OSError("media unavailable")
            return response if url == media else metadata

    def forbidden_cookie():
        raise AssertionError("Mirror requests must not consult Instagram credentials")

    monkeypatch.setattr(bot, "_get_http_session", Session)
    monkeypatch.setattr(bot, "instagram_cookie_header", forbidden_cookie)
    if failure == "cap":
        monkeypatch.setattr(bot, "MAX_FILESIZE_MB", 0)
    result = asyncio.run(bot.download_instagram_via_mirror(POST, str(tmp_path)))
    if failure is None:
        assert result[0] == "video" and Path(result[1]).read_bytes() == b"video"
    else:
        assert result == ("link", None)
    assert [url for url, _ in seen] == [bot.replace_hostname(POST, bot.INSTAGRAM_MIRROR_HOST), media]
    for _, kwargs in seen:
        assert set(map(str.lower, kwargs["headers"])) == {"user-agent"}
        assert "cookies" not in kwargs and "auth" not in kwargs


@pytest.mark.parametrize("proof", ["explicit", "thumbnail"])
@pytest.mark.parametrize("success", [False, True])
def test_mirror_raw_metadata_proof_controls_link_fallback(monkeypatch, tmp_path, proof, success):
    media = "https://media.example/offload/clip?token=AbC"
    tag = (f'<meta property="og:video" content="{media}">' if proof == "explicit" else
           f'<meta property="og:image" content="{media}&amp;thumbnail=1">')
    seen = []

    class Session:
        def get(self, url, **kwargs):
            seen.append(url)
            if url == media:
                return _MediaResp(status=200 if success else 403)
            return _MediaResp(content_type="text/html", body=tag.encode())

    monkeypatch.setattr(bot, "_get_http_session", Session)
    result = asyncio.run(bot.download_instagram_via_mirror(POST, str(tmp_path)))
    if success:
        assert result[0] == "video" and Path(result[1]).read_bytes() == b"video"
    else:
        assert result == (("link", None) if proof == "explicit" else (None, None))
    # The proof classification survives the normal mirror cache, with no reprobe.
    assert asyncio.run(bot.download_instagram_via_mirror(POST, str(tmp_path))) == result
    assert seen == [bot.replace_hostname(POST, bot.INSTAGRAM_MIRROR_HOST), media, media]


@pytest.mark.parametrize("kind", [None, "error", "image"])
def test_unverified_mirror_never_offers_link(monkeypatch, tmp_path, kind):
    async def probe(url):
        return kind, None

    monkeypatch.setattr(bot, "_instagram_mirror_media", probe)
    assert asyncio.run(bot.download_instagram_via_mirror(POST, str(tmp_path))) == (
        "image" if kind == "image" else None, None
    )


@pytest.mark.parametrize("phase", ["recheck", "media"])
def test_mirror_cancellation_propagates(monkeypatch, tmp_path, phase):
    async def probe(*args):
        return (None, None) if phase == "recheck" else ("video", "https://media.example/clip")

    async def cancel_sleep(delay):
        raise asyncio.CancelledError

    class Session:
        def get(self, *args, **kwargs):
            raise asyncio.CancelledError

    monkeypatch.setattr(bot, "_instagram_mirror_media_from_host", probe)
    monkeypatch.setattr(bot.asyncio, "sleep", cancel_sleep)
    if phase == "media":
        monkeypatch.setattr(bot, "_instagram_mirror_media", probe)
        monkeypatch.setattr(bot, "_get_http_session", Session)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.download_instagram_via_mirror(POST, str(tmp_path)))
