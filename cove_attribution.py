from __future__ import annotations

import re


USER_MENTION_RE = re.compile(r"^(?P<mentions>(?:<@!?\d+>\s*)+)(?P<message>.*)$")
USER_MENTION_TOKEN_RE = re.compile(r"<@!?(?P<user_id>\d+)>")


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


def friend_target_post_content(text: str, mention_names: dict[int, str]) -> str:
    text = text.strip()
    if not text:
        return ""
    match = USER_MENTION_RE.match(text)
    if not match:
        return text

    mention_text = match.group("mentions").strip()
    message = match.group("message").strip()
    rendered_mentions = []
    for token_match in USER_MENTION_TOKEN_RE.finditer(mention_text):
        user_id = int(token_match.group("user_id"))
        display_name = mention_names.get(user_id)
        if display_name:
            rendered_mentions.append(f"{token_match.group(0)} {display_name}")
        else:
            rendered_mentions.append(token_match.group(0))

    prefix = " ".join(rendered_mentions)
    if message:
        return f"{prefix}: {message}"
    return prefix
