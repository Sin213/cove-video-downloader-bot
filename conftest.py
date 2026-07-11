import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(__file__))

_BOT_STATE_ATTRS = (
    "_instagram_probe_cache",
    "_instagram_mirror_cache",
    "_ytdlp_info_cache",
    "_reddit_shortlink_cache",
    "_reddit_has_video_cache",
    "_reddit_gallery_cache",
    "_twitter_probe_cache",
    "_user_request_times",
    "_inflight_urls",
    "_cache_write_queue",
    "_arazu_fallback_urls",
)


@pytest.fixture(autouse=True)
def clear_bot_state():
    try:
        import bot
    except Exception:
        yield
        return

    def _clear():
        for attr in _BOT_STATE_ATTRS:
            state = getattr(bot, attr, None)
            if state is not None:
                state.clear()

    _clear()
    yield
    _clear()
