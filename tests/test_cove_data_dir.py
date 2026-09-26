"""Import-time coverage for the optional persistent data directory."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PATHS_SCRIPT = (
    "import bot; print(bot.COOKIES_FILE); print(bot.CACHE_DB_PATH); "
    "print(bot.RUNTIME_SETTINGS_PATH)"
)


def _copy_app(app_dir):
    for name in ("bot.py", "cove_attribution.py"):
        shutil.copy2(REPO_ROOT / name, app_dir / name)


def _run_bot(script, data_dir, *, persistent_cache="0", cwd=REPO_ROOT):
    env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    env.update(
        DISCORD_TOKEN="test-token-not-real",
        GUILD_ID="1",
        FRIEND_GUILD_ID="0",
        PERSISTENT_CACHE=persistent_cache,
        COVE_DATA_DIR=str(data_dir),
        PYTHON_DOTENV_DISABLED="1",
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _assert_config_error(result):
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "[Cove]" in output
    assert "COVE_DATA_DIR" in output
    assert "Traceback" not in output


def test_blank_config_preserves_current_locations(tmp_path):
    _copy_app(tmp_path)
    result = _run_bot(PATHS_SCRIPT, "", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(tmp_path / name)
        for name in ("cookies.txt", "cache.db", "runtime_settings.json")
    ]


def test_host_import_uses_sealed_data_dir():
    import bot

    assert Path(bot._DATA_DIR) != REPO_ROOT
    assert bot.PERSISTENT_CACHE is False


def test_configured_directory_relocates_all_three():
    with tempfile.TemporaryDirectory() as data_dir:
        result = _run_bot(PATHS_SCRIPT, data_dir)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            str(Path(data_dir) / name)
            for name in ("cookies.txt", "cache.db", "runtime_settings.json")
        ]
        assert list(Path(data_dir).iterdir()) == []


def test_configured_directory_preserves_trailing_space():
    with tempfile.TemporaryDirectory() as parent:
        data_dir = Path(parent) / "cove-data "
        data_dir.mkdir()
        result = _run_bot("import bot; print(bot.COOKIES_FILE)", data_dir)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [str(data_dir / "cookies.txt")]


def test_configured_runtime_settings_are_read():
    with tempfile.TemporaryDirectory() as data_dir:
        (Path(data_dir) / "runtime_settings.json").write_text(
            json.dumps({"youtube_quality": "720"}), encoding="utf-8"
        )
        result = _run_bot("import bot; print(bot._youtube_quality)", data_dir)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "720"


def test_cache_file_created_in_configured_directory():
    root_cache = REPO_ROOT / "cache.db"
    existed_before = root_cache.exists()
    with tempfile.TemporaryDirectory() as data_dir:
        result = _run_bot("import bot", data_dir, persistent_cache="1")
        assert result.returncode == 0, result.stderr
        assert (Path(data_dir) / "cache.db").is_file()
    if existed_before:
        assert root_cache.exists()


def test_configured_cookies_are_discovered():
    with tempfile.TemporaryDirectory() as data_dir:
        cookie_file = Path(data_dir) / "cookies.txt"
        cookie_file.write_text("# test cookie file\n", encoding="utf-8")
        result = _run_bot(
            "import bot; print(bot.COOKIES_FILE); print(bot.COOKIES_EXIST)", data_dir
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [str(cookie_file), "True"]


@pytest.mark.parametrize("relative_path", ["data", "./data"])
def test_relative_path_rejected_without_creating_directory(relative_path, tmp_path):
    _copy_app(tmp_path)
    result = _run_bot("import bot", relative_path, cwd=tmp_path)
    _assert_config_error(result)
    assert "must be an absolute path" in result.stdout + result.stderr
    assert not (tmp_path / "data").exists()


def test_missing_absolute_path_rejected_without_creation():
    with tempfile.TemporaryDirectory() as parent:
        missing = Path(parent) / "missing"
        result = _run_bot("import bot", missing)
        _assert_config_error(result)
        assert "does not exist" in result.stderr
        assert not missing.exists()


def test_file_instead_of_directory_rejected():
    with tempfile.NamedTemporaryFile() as data_file:
        result = _run_bot("import bot", data_file.name)
        _assert_config_error(result)
        assert "not a directory" in result.stderr


def test_inaccessible_directory_reports_access_error():
    with tempfile.TemporaryDirectory() as data_dir:
        script = (
            "import bot, os\n"
            "original_stat = os.stat\n"
            f"data_dir = {data_dir!r}\n"
            "def fail_stat(path, *args, **kwargs):\n"
            "    if path == data_dir:\n"
            "        raise PermissionError('test denied')\n"
            "    return original_stat(path, *args, **kwargs)\n"
            "os.stat = fail_stat\n"
            "bot._resolve_data_dir()\n"
        )
        result = _run_bot(script, data_dir)
        _assert_config_error(result)
        assert "cannot be accessed" in result.stderr


def test_write_probe_failure_is_reported_without_debris():
    with tempfile.TemporaryDirectory() as data_dir:
        script = (
            "import tempfile\n"
            "def fail_probe(*args, **kwargs):\n"
            "    raise PermissionError('test denied')\n"
            "tempfile.NamedTemporaryFile = fail_probe\n"
            "import bot\n"
        )
        result = _run_bot(script, data_dir)
        _assert_config_error(result)
        assert "not writable" in result.stderr
        assert list(Path(data_dir).iterdir()) == []
