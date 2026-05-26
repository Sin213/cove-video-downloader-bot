from cove_attribution import friend_post_content


def test_friend_post_content_keeps_plain_message_after_poster():
    assert friend_post_content("Sin", "this u Eat Hat") == "Sin: this u Eat Hat"


def test_friend_post_content_places_message_after_poster_and_mention():
    assert friend_post_content("Sin", "<@123456789> this u") == "Sin <@123456789>: this u"


def test_friend_post_content_keeps_multiple_mentions_before_message():
    assert (
        friend_post_content("Sin", "<@123456789> <@987654321> this u")
        == "Sin <@123456789> <@987654321>: this u"
    )


def test_friend_post_content_keeps_discord_mention_without_message():
    assert friend_post_content("Sin", "<@123456789>") == "Sin <@123456789>"


def test_friend_post_content_without_mentions_is_plaintext_poster_only():
    assert friend_post_content("Sin", "") == "Sin"
