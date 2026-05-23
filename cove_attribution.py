from __future__ import annotations


def friend_post_content(display_name: str, extra_mentions: str) -> str:
    extra_mentions = extra_mentions.strip()
    if extra_mentions:
        return f"{extra_mentions} {display_name}"
    return display_name
