"""FRIEND_GUILD_IDS parsing and is_friend_server membership."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_parse(env_extra: dict[str, str]) -> str:
    env = os.environ.copy()
    env.update(
        {
            "DISCORD_TOKEN": "test-token-not-real",
            "GUILD_ID": "1",
            "PERSISTENT_CACHE": "0",
            "FRIEND_GUILD_ID": "0",
            "FRIEND_GUILD_IDS": "",
        }
    )
    env.update(env_extra)
    script = (
        "from bot import parse_friend_guild_ids, FRIEND_GUILD_IDS, is_friend_server\n"
        "import bot\n"
        "print(','.join(str(i) for i in sorted(FRIEND_GUILD_IDS)))\n"
        "print(is_friend_server(type('G', (), {'id': 110})()))\n"
        "print(is_friend_server(type('G', (), {'id': 220})()))\n"
        "print(is_friend_server(None))\n"
    )
    # Prefer testing the pure parser + live module constants separately
    script = (
        "from bot import parse_friend_guild_ids\n"
        f"ids = parse_friend_guild_ids("
        f"friend_guild_id={env_extra.get('FRIEND_GUILD_ID', '0')!r}, "
        f"friend_guild_ids={env_extra.get('FRIEND_GUILD_IDS', '')!r})\n"
        "print('IDS=' + ','.join(str(i) for i in sorted(ids)))\n"
    )
    out = subprocess.check_output(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env={**env, "DISCORD_TOKEN": "x", "GUILD_ID": "1"},
        text=True,
    )
    for line in out.splitlines():
        if line.startswith("IDS="):
            return line[4:]
    raise AssertionError(f"no IDS= line in output: {out!r}")


def test_parse_singular_only():
    assert _run_parse({"FRIEND_GUILD_ID": "110", "FRIEND_GUILD_IDS": ""}) == "110"


def test_parse_list_only():
    assert _run_parse({"FRIEND_GUILD_ID": "0", "FRIEND_GUILD_IDS": "110,220"}) == "110,220"


def test_parse_union_singular_and_list():
    assert _run_parse({"FRIEND_GUILD_ID": "110", "FRIEND_GUILD_IDS": "220,330"}) == "110,220,330"


def test_parse_ignores_zero_and_junk():
    assert _run_parse({"FRIEND_GUILD_ID": "0", "FRIEND_GUILD_IDS": "0, abc, 440,,"}) == "440"


def test_parse_empty_means_no_friend_guilds():
    assert _run_parse({"FRIEND_GUILD_ID": "0", "FRIEND_GUILD_IDS": ""}) == ""


def test_is_friend_server_membership_via_subprocess():
    env = os.environ.copy()
    env.update(
        {
            "DISCORD_TOKEN": "test-token-not-real",
            "GUILD_ID": "1",
            "FRIEND_GUILD_ID": "110",
            "FRIEND_GUILD_IDS": "220",
            "PERSISTENT_CACHE": "0",
        }
    )
    script = (
        "from bot import is_friend_server\n"
        "G = type('G', (), {})\n"
        "print(is_friend_server(type('G', (), {'id': 110})()))\n"
        "print(is_friend_server(type('G', (), {'id': 220})()))\n"
        "print(is_friend_server(type('G', (), {'id': 999})()))\n"
        "print(is_friend_server(None))\n"
    )
    out = subprocess.check_output(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        text=True,
    )
    lines = [l.strip() for l in out.strip().splitlines() if l.strip() in {"True", "False"}]
    # Logging may precede; take last 4 bools
    bools = lines[-4:]
    assert bools == ["True", "True", "False", "False"]
