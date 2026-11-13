"""Titles and previews are cut at a word boundary, and say so."""
from emporia import relay_server  # noqa: F401 — the routes import it first
from emporia.routes.happening import first_line
from emporia.routes.posts import topic_post_view
from emporia.textcut import cut_at_word


def test_short_text_is_left_alone_and_long_text_is_cut_at_a_word():
    assert cut_at_word("short one", 20) == "short one"
    assert cut_at_word("x" * 20, 20) == "x" * 20, "fits exactly"
    assert cut_at_word("the quick brown fox jumps", 12) == "the quick…", "not 'the quick br…'"
    assert cut_at_word("the quick brown fox", 16) == "the quick brown…", "a space right after the room keeps the word"
    assert cut_at_word("the quick brown fox", 15) == "the quick…"
    assert cut_at_word("", 5) == "" and cut_at_word(None, 5) == ""


def test_a_long_word_with_no_space_is_cut_at_the_limit():
    assert cut_at_word("a" * 50, 10) == "a" * 9 + "…"
    assert cut_at_word("supercalifragilistic and more", 8) == "superca…"


def test_the_result_with_its_ellipsis_never_exceeds_the_limit():
    for text in ("the quick brown fox jumps over", "a" * 50, "ab cd ef gh ij", "x y z " * 10):
        for limit in range(1, 40):
            assert len(cut_at_word(text, limit)) <= limit, (text, limit)


def test_the_titles_a_post_and_the_news_derive_use_it():
    text = "Rules: " + "word " * 30
    title = topic_post_view({"kind": "note", "text": text, "post_id": "p", "author_id": "a", "label": "",
                             "likes": 0, "replies": 0, "at": "t", "nonce": "n", "signature": "s", "tags": [],
                             "reply_to": None, "root": None, "quote_of": None, "reactions": {}, "tool_marks": []},
                            {"topic_id": "t", "slug": "s", "name": "S"})["title"]
    assert title.endswith("word…") and len(title) <= 81
    cut = first_line("one two three " * 20)
    assert cut.endswith("…") and len(cut) <= 111 and cut[:-1].split()[-1] in ("one", "two", "three")
    assert first_line("short") == "short"
