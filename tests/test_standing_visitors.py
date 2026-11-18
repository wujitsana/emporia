"""The visitor home check pulls the home relay's standing statement; the host shows it as evidence, never counted."""
import time

import httpx
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia import standing_statement as ss
from emporia import visitors
from tests import test_cross_relay_visitors as V
from tests.test_standing_statement import FakeRelay


def _home_with_standing(monkeypatch, pub_key, relay, *, tamper=False, counted=3):
    """The home relay lists the visitor and answers its standing statement (signed by `relay`, not by this relay)."""
    asked = V.fake_home(monkeypatch, [pub_key], relay_id=relay.id)
    listed = httpx.get

    def get(url, *a, **kw):
        if "/standing-statement" in str(url):
            asked.append(str(url))
            st = relay.statement(subject_key=pub_key, now=time.time(), counted=counted, audience=rs.relay_id())
            st["issuer"]["relay_url"] = V.HOME
            st["signature"] = relay.key.sign(ss.signable(st)).hex()
            if tamper:
                st["facts"]["counted_operators"] = 50
            return httpx.Response(200, json=st, request=httpx.Request("GET", url))
        return listed(url, *a, **kw)
    monkeypatch.setattr(httpx, "get", get)
    visitors.forget()
    return asked


def test_the_home_check_keeps_the_homes_statement_and_the_profile_shows_it_as_not_counted(monkeypatch):
    c = TestClient(rs.app, client=("127.0.0.1", 51291))
    monkeypatch.setenv("EMPORIA_RELAY_SIGN_IN_ORIGINS", V.ORIGIN)
    monkeypatch.delenv("EMPORIA_VISITORS", raising=False)
    relay = FakeRelay(V.HOME)
    a = V.name("sv")
    asked = _home_with_standing(monkeypatch, V.pub_of(a), relay)
    assert V.register(c, a, V.visiting(V.HOME, relay.id)).status_code == 200
    assert any("/standing-statement?audience=" + rs.relay_id() in u for u in asked)
    viewer = V.name("viewer")
    V.register(c, viewer)
    profile = c.get(f"/agents/{a}", headers=V._hdr(viewer)).json()
    [shown] = profile["standing_elsewhere"]
    assert shown["verified"] is True and shown["counted_here"] is False
    assert shown["line"].startswith(f"standing at {V.HOME}:") and "verified: signed by relay " in shown["line"]
    assert "not counted here" in shown["line"]
    assert c.get(f"/agents/{a}/passport").json()["reach"]["relays"] == 1      # Reach is evidence, not a score
    assert profile["presence"]["check"]["status"] == "confirmed"


def test_a_tampered_statement_from_the_home_is_dropped_and_blocks_nothing(monkeypatch):
    c = TestClient(rs.app, client=("127.0.0.1", 51292))
    monkeypatch.setenv("EMPORIA_RELAY_SIGN_IN_ORIGINS", V.ORIGIN)
    monkeypatch.delenv("EMPORIA_VISITORS", raising=False)
    relay = FakeRelay(V.HOME)
    a = V.name("sw")
    _home_with_standing(monkeypatch, V.pub_of(a), relay, tamper=True)
    assert V.register(c, a, V.visiting(V.HOME, relay.id)).status_code == 200
    viewer = V.name("viewer")
    V.register(c, viewer)
    profile = c.get(f"/agents/{a}", headers=V._hdr(viewer)).json()
    assert profile["standing_elsewhere"] == [] and profile["presence"]["check"]["status"] == "confirmed"
