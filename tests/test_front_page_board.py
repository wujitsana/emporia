"""The relay's public front page lists the top season standings, escaped, and omits them when there are none."""
import uuid

from fastapi.testclient import TestClient

from emporia import quest_engine as E
from emporia import relay_server as rs
from emporia.routes import standing as ST


def _per(names):
    c = E.Ctx(operator=lambda x: x)
    games = [{"session_id": f"s{i}", "module_type": "emporia:blotto:v2", "participants": [a, "zz"],
              "ended_at": "2026-11-03T10:00:00+00:00", "winner": None, "termination": "rounds"}
             for i, a in enumerate(names)]
    per = {a: (E.events(a, games=games, ctx=c), games) for a in [*names, "zz"]}
    return per, c, []


def _setup(monkeypatch, per):
    monkeypatch.setenv("EMPORIA_PUBLIC_CACHE_SECONDS", "0")
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    monkeypatch.setenv("EMPORIA_SEASON", f"fp-{uuid.uuid4().hex[:6]}")
    monkeypatch.setenv("EMPORIA_SEASON_START", "2020-01-01T00:00:00+00:00")
    monkeypatch.setenv("EMPORIA_SEASON_END", "2999-01-01T00:00:00+00:00")
    monkeypatch.setattr(ST, "season_inputs", lambda: per)


def test_front_page_shows_standings_and_escapes_names(monkeypatch):
    _setup(monkeypatch, _per(["alice", "<script>x</script>"]))
    page = TestClient(rs.app).get("/").text
    assert "Season standings" in page and "alice" in page and "XP" in page and "level" in page
    assert "<script>x" not in page and "&lt;script&gt;x" in page
    assert page.index("Season standings") < page.index("Join with Hermes, in a terminal:")
    assert 'href="/season/board"' in page
    assert "any agent" in page and "stores only ciphertext" not in page and 'href="/agents.txt"' in page


def test_front_page_omits_standings_when_module_off(monkeypatch):
    _setup(monkeypatch, _per(["alice"]))
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    page = TestClient(rs.app).get("/").text
    assert "Season standings" not in page and "Join with Hermes, in a terminal:" in page


def test_front_page_omits_standings_when_nobody_has_xp(monkeypatch):
    _setup(monkeypatch, ({}, E.Ctx(operator=lambda x: x), []))
    assert "Season standings" not in TestClient(rs.app).get("/").text
