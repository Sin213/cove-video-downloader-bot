from cove_attribution import friend_post_content


def test_friend_post_content_pings_tagged_user_and_shows_plaintext_poster():
    assert friend_post_content("Sin", "@Eat Hat") == "@Eat Hat Sin"


def test_friend_post_content_keeps_discord_mention_before_plaintext_poster():
    assert friend_post_content("Sin", "<@123456789>") == "<@123456789> Sin"


def test_friend_post_content_without_mentions_is_plaintext_poster_only():
    assert friend_post_content("Sin", "") == "Sin"
