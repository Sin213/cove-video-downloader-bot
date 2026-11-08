import pytest

import bot


@pytest.mark.parametrize(
    "url",
    [
        "https://www.twitch.tv/joana",
        "https://twitch.tv/joana/",
        "https://m.twitch.tv/joana",
        "https://www.twitch.tv/joana/videos",
    ],
)
def test_twitch_channel_links_ignored(url):
    assert bot.extract_supported_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://clips.twitch.tv/SomeClipSlug",
        "https://www.twitch.tv/joana/clip/SomeClipSlug",
        "https://www.twitch.tv/videos/123456789",
    ],
)
def test_twitch_clips_and_vods_still_handled(url):
    assert bot.extract_supported_url(url) == url
