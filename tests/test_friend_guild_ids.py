"""Friend guild configuration, command registration, and startup sync behavior."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_bot(script, *, singular="0", plural=None, primary="111"):
    with tempfile.TemporaryDirectory() as data_dir:
        env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
        env.update(
            DISCORD_TOKEN="test-token-not-real",
            GUILD_ID=primary,
            FRIEND_GUILD_ID=singular,
            COVE_DATA_DIR=data_dir,
            PERSISTENT_CACHE="0",
            PYTHON_DOTENV_DISABLED="1",
            PYTHONDONTWRITEBYTECODE="1",
        )
        if plural is not None:
            env["FRIEND_GUILD_IDS"] = plural
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


def _json_result(script, **config):
    result = _run_bot(script, **config)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


CONFIG_SCRIPT = (
    "import bot, json; "
    "print(json.dumps(sorted(bot.EFFECTIVE_FRIEND_GUILD_IDS)))"
)


@pytest.mark.parametrize(
    ("singular", "plural", "expected"),
    [
        ("0", None, []),
        ("222", None, [222]),
        ("0", "222,333", [222, 333]),
        ("222", "333", [222, 333]),
        ("222", "222,333,333", [222, 333]),
        ("0", " 222 , 333 ", [222, 333]),
        ("0", "0,222,0", [222]),
        ("0", "222,,333,", [222, 333]),
        ("0", "   ", []),
    ],
)
def test_effective_friend_guild_ids(singular, plural, expected):
    assert _json_result(CONFIG_SCRIPT, singular=singular, plural=plural) == expected


@pytest.mark.parametrize("plural", ["222,abc", "222,-3"])
def test_invalid_plural_ids_rejected_at_import(plural):
    result = _run_bot("import bot", plural=plural)
    assert result.returncode != 0
    assert result.stderr.strip() == "[Cove] Env var FRIEND_GUILD_IDS must contain only non-negative integers."


MEMBERSHIP_SCRIPT = """
import bot
import discord
import json
print(json.dumps([bot.is_friend_server(discord.Object(id=g)) for g in (222, 333, 444)]
                 + [bot.is_friend_server(None)]))
"""


def test_membership_for_every_friend_and_no_other_guild():
    assert _json_result(MEMBERSHIP_SCRIPT, plural="222,333") == [True, True, False, False]


def test_singular_only_keeps_legacy_membership():
    assert _json_result(MEMBERSHIP_SCRIPT, singular="222") == [True, False, False, False]


SYNC_SCRIPT = """
import asyncio
import bot
import json
from unittest.mock import AsyncMock, patch

async def check():
    def close_background_task(coro):
        coro.close()

    with patch.object(bot, '_sweep_orphaned_tmpdirs'), \\
         patch.object(bot, 'spawn_tracked', side_effect=close_background_task), \\
         patch.object(bot.client.tree, 'copy_global_to') as copy, \\
         patch.object(bot.client, '_sync_tree_with_timeout', new_callable=AsyncMock) as sync:
        await bot.client.setup_hook()
        return {
            'copy': [call.kwargs['guild'].id for call in copy.call_args_list],
            'sync': [[call.args[0].id, call.args[1]] for call in sync.call_args_list],
        }

loop = asyncio.new_event_loop()
try:
    print(json.dumps(loop.run_until_complete(check())))
finally:
    loop.close()
"""


def test_primary_friend_overlap_is_member_but_synced_once():
    script = "import bot, discord; assert bot.is_friend_server(discord.Object(id=111))\n" + SYNC_SCRIPT
    assert _json_result(script, singular="111", plural="111,222") == {
        "copy": [111, 222],
        "sync": [[111, "Primary"], [222, "Friend"]],
    }


def test_syncs_each_distinct_friend_once_in_order():
    assert _json_result(SYNC_SCRIPT, singular="333", plural="444,222,333,222") == {
        "copy": [111, 222, 333, 444],
        "sync": [[111, "Primary"], [222, "Friend"], [333, "Friend"], [444, "Friend"]],
    }


REGISTRATION_SCRIPT = """
import bot
import discord
import json
print(json.dumps([bot.client.tree.get_command('neet', guild=discord.Object(id=g)) is not None
                  for g in (111, 222, 333, 444)]))
"""


@pytest.mark.parametrize(
    ("singular", "plural", "expected"),
    [
        ("0", None, [False, False, False, False]),
        ("0", "222,333", [False, True, True, False]),
        ("222", "333", [False, True, True, False]),
    ],
)
def test_neet_registration(singular, plural, expected):
    assert _json_result(REGISTRATION_SCRIPT, singular=singular, plural=plural) == expected


HELP_SCRIPT = """
import asyncio
import bot
import discord
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

async def check(guild_id):
    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(guild=discord.Object(id=guild_id), response=response)
    await bot.help_cmd.callback(interaction)
    return '`/neet`' in response.send_message.call_args.args[0]

print(json.dumps(asyncio.run(check(222)) and asyncio.run(check(333))
                 and not asyncio.run(check(444))))
"""


def test_help_shows_neet_for_each_plural_friend():
    assert _json_result(HELP_SCRIPT, plural="222,333") is True


NEET_SCRIPT = """
import asyncio
import bot
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

async def arm(guild_id, user_id):
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=guild_id),
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    await bot.neet_cmd.callback(interaction)

def message(guild_id, user_id):
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id, bot=False),
        guild=SimpleNamespace(id=guild_id),
        reference=None,
        content='hello',
    )

def armed():
    return sorted([list(key) for key in bot._friend_neet_skip_users])
"""


def test_neet_skip_consumed_by_next_message_in_same_guild():
    script = NEET_SCRIPT + """
async def check():
    await arm(222, 9001)
    before = armed()
    await bot.client.on_message(message(222, 9001))
    return [before, armed()]

print(json.dumps(asyncio.run(check())))
"""
    assert _json_result(script, plural="222,333") == [[[222, 9001]], []]


def test_neet_skip_survives_message_in_other_friend_guild():
    script = NEET_SCRIPT + """
async def check():
    await arm(222, 9001)
    before = armed()
    await bot.client.on_message(message(333, 9001))
    after_other_guild = armed()
    await bot.client.on_message(message(222, 9001))
    return [before, after_other_guild, armed()]

print(json.dumps(asyncio.run(check())))
"""
    assert _json_result(script, plural="222,333") == [
        [[222, 9001]], [[222, 9001]], []
    ]


def test_neet_skips_for_same_user_in_two_guilds_are_independent():
    script = NEET_SCRIPT + """
async def check():
    await arm(222, 9001)
    await arm(333, 9001)
    before = armed()
    await bot.client.on_message(message(222, 9001))
    after_first = armed()
    await bot.client.on_message(message(333, 9001))
    return [before, after_first, armed()]

print(json.dumps(asyncio.run(check())))
"""
    assert _json_result(script, plural="222,333") == [
        [[222, 9001], [333, 9001]], [[333, 9001]], []
    ]


def test_neet_skips_for_two_users_in_same_guild_are_independent():
    script = NEET_SCRIPT + """
async def check():
    await arm(222, 9001)
    await arm(222, 9002)
    before = armed()
    await bot.client.on_message(message(222, 9001))
    after_first = armed()
    await bot.client.on_message(message(222, 9002))
    return [before, after_first, armed()]

print(json.dumps(asyncio.run(check())))
"""
    assert _json_result(script, plural="222,333") == [
        [[222, 9001], [222, 9002]], [[222, 9002]], []
    ]


def test_prune_neet_skips_removes_only_expired_guild_user_entry():
    script = NEET_SCRIPT + """
async def check():
    await arm(222, 9001)
    await arm(333, 9002)
    bot._friend_neet_skip_users[(222, 9001)] = bot.monotonic() - 1
    before = armed()
    bot.prune_neet_skips()
    after_prune = armed()
    await bot.client.on_message(message(333, 9002))
    return [before, after_prune, armed()]

print(json.dumps(asyncio.run(check())))
"""
    assert _json_result(script, plural="222,333") == [
        [[222, 9001], [333, 9002]], [[333, 9002]], []
    ]
