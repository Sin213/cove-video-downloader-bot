import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def clear_probe_caches():
    try:
        import bot
    except Exception:
        yield
        return
    bot._instagram_probe_cache.clear()
    yield
    bot._instagram_probe_cache.clear()
