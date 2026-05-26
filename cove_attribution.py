from __future__ import annotations

import re


USER_MENTION_RE = re.compile(r"^(?P<mentions>(?:<@!?\d+>\s*)+)(?P<message>.*)$")


def friend_post_content(display_name: str, extra_mentions: str) -> str:
    text = extra_mentions.strip()
    if text:
        match = USER_MENTION_RE.match(text)
        if match:
            mentions = match.group("mentions").strip()
            message = match.group("message").strip()
            if message:
                return f"{display_name} {mentions}: {message}"
            return f"{display_name} {mentions}"
        return f"{display_name}: {text}"
    return display_name
