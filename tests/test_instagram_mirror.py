import asyncio

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
