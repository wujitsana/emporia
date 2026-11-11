"""A topic's post count is its posts, counted: the stored column was never kept up to date (every topic read 0)."""
from emporia.routes.agoras import _post_counts


def test_post_counts_are_top_level_undeleted_posts_per_topic():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE posts (topic TEXT, reply_to TEXT, is_deleted INTEGER NOT NULL DEFAULT 0)")
    conn.executemany("INSERT INTO posts VALUES (?,?,?)", [
        ("ideas", None, 0), ("ideas", None, 0), ("ideas", "post:x", 0), ("ideas", None, 1),
        ("games", None, 0), (None, None, 0)])
    assert _post_counts(conn, ["ideas", "games", "help"]) == {"ideas": 2, "games": 1}
    assert _post_counts(conn, []) == {}
