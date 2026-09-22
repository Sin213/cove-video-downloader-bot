"""COVE_DATA_DIR relocates cookies / cache / runtime settings (Docker volumes)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_cove_data_dir_relocates_persistent_paths(tmp_path: Path) -> None:
    data_dir = tmp_path / "cove-data"
    data_dir.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "DISCORD_TOKEN": "test-token-not-real",
            "GUILD_ID": "1",
            "FRIEND_GUILD_ID": "0",
            "COVE_DATA_DIR": str(data_dir),
            "PERSISTENT_CACHE": "0",
        }
    )
    # Fresh interpreter so import-time path constants pick up the env var.
    script = (
        "import bot;"
        "print(bot._DATA_DIR);"
        "print(bot.COOKIES_FILE);"
        "print(bot.CACHE_DB_PATH);"
        "print(bot.RUNTIME_SETTINGS_PATH)"
    )
    out = subprocess.check_output(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
    )
    lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
    # Logging may precede prints; take the last four path lines.
    paths = lines[-4:]
    assert paths[0] == str(data_dir.resolve())
    assert paths[1] == str(data_dir.resolve() / "cookies.txt")
    assert paths[2] == str(data_dir.resolve() / "cache.db")
    assert paths[3] == str(data_dir.resolve() / "runtime_settings.json")
