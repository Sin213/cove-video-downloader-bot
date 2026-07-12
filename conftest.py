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
    "_deletable",
    "_friend_posts",
    "_friend_neet_skip_users",
    "_processed_source_messages",
    "_active_tasks",
)

_BOT_SCALAR_DEFAULTS = {
    "_queued_jobs": 0,
    "_user_rate_last_sweep": 0,
    "_ytdlp_admin_warning_sent": False,
    "_reddit_cookie_header_cache": None,
}


@pytest.fixture(autouse=True, scope="session")
def isolate_cache_db(tmp_path_factory):
    """Point the persistent cache at a throwaway DB so tests never touch the
    repo's real cache.db (the import-time _init_persistent_cache connection)."""
    try:
        import bot
    except BaseException:
        yield
        return
    if bot._cache_db_conn is not None:
        bot._cache_db_conn.close()
    bot._cache_db_conn = None
    bot.CACHE_DB_PATH = str(tmp_path_factory.mktemp("cove_cache") / "cache.db")
    yield


@pytest.fixture(autouse=True)
def clear_bot_state():
    try:
        import bot
    except BaseException:
        yield
        return

    def _clear():
        for attr in _BOT_STATE_ATTRS:
            state = getattr(bot, attr, None)
            if state is not None:
                state.clear()
        for attr, default in _BOT_SCALAR_DEFAULTS.items():
            if hasattr(bot, attr):
                setattr(bot, attr, default)

    _clear()
    yield
    _clear()
