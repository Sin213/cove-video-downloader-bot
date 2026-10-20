import asyncio
from unittest.mock import Mock

import pytest

import bot


URL = "https://www.reddit.com/r/test/comments/abc/title/"
MALFORMED = "https://[invalid/x.jpg"
RAW_IMAGES = [
    "https://preview.redd.it/a.jpg?width=960&amp;s=1",
    "https://preview.redd.it/b.jpg?width=960&amp;s=2",
]
IMAGES = [
    "https://preview.redd.it/a.jpg?width=960&s=1",
    "https://preview.redd.it/b.jpg?width=960&s=2",
]
DECOYS = [
    "https://preview.redd.it.evil.example/x.jpg",
    "https://evil.example/preview.redd.it/x.jpg",
    "https://evil.example/x.jpg?u=https://i.redd.it/a.jpg",
    "https://evil.example/x.jpg#https://i.redd.it/a.jpg",
]


def gallery_post(urls, source_key="u"):
    return {
        "is_video": False,
        "url": "https://www.reddit.com/gallery/abc",
        "gallery_data": {
            "items": [{"media_id": f"m{i}"} for i in range(len(urls))],
        },
        "media_metadata": {
            f"m{i}": {"s": {source_key: url}} for i, url in enumerate(urls)
        },
    }


@pytest.fixture
def offline_api(monkeypatch):
    no_http = Mock(side_effect=AssertionError("Unexpected HTTP session access"))
    monkeypatch.setattr(bot, "_get_http_session", no_http)

    def install(post):
        calls = []

        async def fake_get_json(url, timeout):
            calls.append((url, timeout))
            return 200, [{"data": {"children": [{"data": post}]}}]

        monkeypatch.setattr(bot, "_reddit_api_get_json", fake_get_json)
        return calls

    yield install
    no_http.assert_not_called()


@pytest.mark.parametrize("url", [
    MALFORMED,
    "https://[invalid/",
    "https://[::1/x.jpg",
    "https://[/x.jpg",
])
def test_gallery_validator_rejects_malformed_urls(url):
    assert bot._is_valid_reddit_gallery_url(url) is False


@pytest.mark.parametrize("url, expected", [
    (IMAGES[0], True),
    ("https://preview.redd.it:99999999/a.jpg", True),
    ("https://evil.example/a.jpg", False),
    ("http://preview.redd.it/a.jpg", False),
    ("https://preview.redd.it/a.txt", False),
] + [(url, False) for url in DECOYS])
def test_gallery_validator_parseable_urls_unchanged(url, expected):
    assert bot._is_valid_reddit_gallery_url(url) is expected


@pytest.mark.parametrize("position", [0, 1], ids=["first", "middle"])
@pytest.mark.parametrize("source_key", ["u", "gif"])
def test_gallery_post_skips_malformed_items(position, source_key):
    urls = list(RAW_IMAGES)
    urls.insert(position, MALFORMED)
    assert bot.reddit_gallery_image_urls_from_post(gallery_post(urls, source_key)) == IMAGES


@pytest.mark.parametrize("source_key", ["u", "gif"])
def test_gallery_post_requires_two_valid_images(source_key):
    post = gallery_post([RAW_IMAGES[0], MALFORMED], source_key)
    assert bot.reddit_gallery_image_urls_from_post(post) == []


@pytest.mark.parametrize("source_key", ["u", "gif"])
def test_has_video_skips_malformed_gallery_and_caches_false(offline_api, source_key):
    post = gallery_post([RAW_IMAGES[0], MALFORMED, RAW_IMAGES[1]], source_key)
    calls = offline_api(post)
    api_url = bot.reddit_api_url(URL)

    assert asyncio.run(bot.reddit_has_video(URL)) is False
    assert bot._cache_get(bot._reddit_has_video_cache, api_url) is False
    assert calls == [(api_url, bot.REDDIT_PRECHECK_TIMEOUT)]
    assert asyncio.run(bot.reddit_has_video(URL)) is False
    assert calls == [(api_url, bot.REDDIT_PRECHECK_TIMEOUT)]

    bot._reddit_gallery_cache.clear()
    assert asyncio.run(bot.reddit_gallery_image_urls(URL)) == IMAGES
    assert calls == [(api_url, bot.REDDIT_PRECHECK_TIMEOUT)] * 2


@pytest.mark.parametrize("source_key", ["u", "gif"])
def test_gallery_api_keeps_valid_images_after_malformed_item(offline_api, source_key):
    calls = offline_api(gallery_post([MALFORMED, *RAW_IMAGES], source_key))

    assert asyncio.run(bot.reddit_gallery_image_urls(URL)) == IMAGES
    assert calls == [(bot.reddit_api_url(URL), bot.REDDIT_PRECHECK_TIMEOUT)]


def test_valid_only_gallery_callers_unchanged(offline_api):
    calls = offline_api(gallery_post(RAW_IMAGES))

    assert asyncio.run(bot.reddit_has_video(URL)) is False
    assert asyncio.run(bot.reddit_gallery_image_urls(URL)) == IMAGES
    assert calls == [(bot.reddit_api_url(URL), bot.REDDIT_PRECHECK_TIMEOUT)]


@pytest.mark.parametrize("include_malformed", [False, True])
def test_gallery_post_rejects_lookalike_hosts(include_malformed):
    urls = [RAW_IMAGES[0], *DECOYS, RAW_IMAGES[1]]
    if include_malformed:
        urls.insert(0, MALFORMED)
    assert bot.reddit_gallery_image_urls_from_post(gallery_post(urls)) == IMAGES


def test_gallery_log_skips_malformed_token():
    log_text = f"{bot.REDDIT_GALLERY_MARKER}\n{MALFORMED} {' '.join(IMAGES)}\n"
    assert bot.reddit_gallery_urls_from_log(log_text) == IMAGES
