import asyncio
import shutil
from unittest.mock import AsyncMock

import pytest

import bot


MALFORMED_DIRECT = "https://[invalid/x.jpg"
MALFORMED_MEDIA = (
    "https://www.reddit.com/media?url=https%3A%2F%2F%5Binvalid%2Fx.gif"
)
VALID_GIF = "https://i.redd.it/ok.gif"


@pytest.mark.parametrize("helper", [
    bot.reddit_media_gif_url_from_text,
    bot.reddit_media_image_url_from_text,
])
@pytest.mark.parametrize("text", [
    MALFORMED_DIRECT,
    MALFORMED_MEDIA,
    "ordinary non-URL text",
    "https://example.com/x.jpg",
    "https://example.com/x.gif",
    "https://example.com/media?url=https%3A%2F%2Fi.redd.it%2Fok.gif",
    "https://example.com/media?url=https%3A%2F%2Fi.redd.it%2Fok.jpg",
    "https://i.redd.it/x.mp4",
])
def test_reddit_media_ignored_text(helper, text):
    assert helper(text) is None


@pytest.mark.parametrize("helper, valid", [
    (bot.reddit_media_gif_url_from_text, VALID_GIF),
    (bot.reddit_media_image_url_from_text, "https://i.redd.it/ok.jpg"),
])
@pytest.mark.parametrize("malformed", [MALFORMED_DIRECT, MALFORMED_MEDIA])
@pytest.mark.parametrize("separator", [" ", "\n"])
@pytest.mark.parametrize("valid_first", [False, True])
def test_reddit_media_skips_malformed_candidates(helper, valid, malformed, separator, valid_first):
    candidates = [valid, malformed] if valid_first else [malformed, valid]
    assert helper(separator.join(candidates)) == valid


@pytest.mark.parametrize("helper, extension", [
    (bot.reddit_media_gif_url_from_text, "gif"),
    (bot.reddit_media_image_url_from_text, "jpg"),
])
def test_reddit_media_skips_malformed_query_part(helper, extension):
    valid = f"https://i.redd.it/ok.{extension}"
    assert helper(f"{MALFORMED_MEDIA}&url={valid}") == valid


@pytest.mark.parametrize("host", ["i.redd.it", "preview.redd.it"])
@pytest.mark.parametrize("extension", ["jpg", "jpeg", "png", "webp"])
def test_reddit_media_direct_images_unchanged(host, extension):
    url = f"https://{host}/ok.{extension}"
    assert bot.reddit_media_image_url_from_text(url) == url
    assert bot.reddit_media_gif_url_from_text(url) is None


def test_reddit_media_encoded_gif_unchanged():
    text = (
        "ERROR: Unsupported URL: https://www.reddit.com/media?url="
        "https%3A%2F%2Fi.redd.it%2Fisd965xyhwsf1.gif"
    )
    assert bot.reddit_media_gif_url_from_text(text) == "https://i.redd.it/isd965xyhwsf1.gif"
    assert bot.reddit_media_image_url_from_text(text) is None


@pytest.mark.parametrize("helper, url", [
    (bot.reddit_media_gif_url_from_text, VALID_GIF),
    (bot.reddit_media_image_url_from_text, "https://preview.redd.it/ok.PNG"),
])
def test_reddit_media_first_valid_and_trailing_punctuation_unchanged(helper, url):
    assert helper(f"{url}).,>;:!?'\"]}} https://i.redd.it/later.gif") == url


@pytest.fixture
def caller_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "TMP_BASE", str(tmp_path))
    created = []
    make_tmpdir = bot._make_job_tmpdir

    def tracked_tmpdir(*args, **kwargs):
        path = make_tmpdir(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(bot, "_make_job_tmpdir", tracked_tmpdir)
    monkeypatch.setattr(bot, "reddit_has_video", AsyncMock(return_value=True))
    before = {path for path in tmp_path.glob("cove_*") if path.is_dir()}
    try:
        yield tmp_path, before, created
    finally:
        # RED runs expose the leak; remove only directories created by this test.
        for path in created:
            shutil.rmtree(path, ignore_errors=True)
            bot._active_tmpdirs.pop(path, None)


@pytest.mark.parametrize("warning", [
    "",
    "WARNING: [generic] Falling back on generic information extractor\n",
])
@pytest.mark.parametrize("valid_after", [False, True])
def test_process_url_reddit_malformed_media_silently_ignored(
    monkeypatch, caller_environment, warning, valid_after,
):
    output = f"{warning}ERROR: Unsupported URL: {MALFORMED_MEDIA}"
    if valid_after:
        output += f"\n{VALID_GIF}"
    run_subprocess = AsyncMock(return_value=(1, output))
    monkeypatch.setattr(bot, "run_subprocess", run_subprocess)
    on_success = AsyncMock()
    on_error = AsyncMock()
    on_too_big = AsyncMock()
    on_no_video = AsyncMock()

    asyncio.run(bot.process_url(
        MALFORMED_MEDIA, None, on_success, on_error, on_too_big, on_no_video,
    ))

    run_subprocess.assert_awaited_once()
    on_error.assert_not_awaited()
    on_success.assert_not_awaited()
    on_too_big.assert_not_awaited()
    on_no_video.assert_awaited_once()
    log_text = on_no_video.await_args.args[0]
    assert "[NOVIDEO]" in log_text
    assert bot.reddit_gif_url_from_log(log_text) == (VALID_GIF if valid_after else None)
    root, before, created = caller_environment
    assert created
    after = {path for path in root.glob("cove_*") if path.is_dir()}
    assert after == before
