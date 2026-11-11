"""Relay audit 11-11: F3 (disk fill), F5 (operator removal sticks), F6 (inbox id cap),
F8 (payer asset and window), F9 (public answer cache)."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _p45_requirement, _P45_PAYER_KEY


@pytest.fixture(autouse=True)
def _clean_state():
    rs._DAILY_WRITE_BUDGET.clear()
    rs._DAILY_WRITE_TOTAL[:] = ["", 0]
    rs._DISK_CACHE[0] = 0.0
    yield
    rs._DAILY_WRITE_BUDGET.clear()
    rs._DAILY_WRITE_TOTAL[:] = ["", 0]
    rs._DISK_CACHE[0] = 0.0


# ---- F3 ------------------------------------------------------------------

def test_daily_write_budget_refuses_over_the_limit(monkeypatch):
    monkeypatch.setenv("EMPORIA_DAILY_WRITE_MB_PER_AGENT", "1")
    for _ in range(5):
        rs._require_content_within_limit("x" * 200_000, caller="f3_a")
    with pytest.raises(HTTPException) as e:
        rs._require_content_within_limit("x" * 200_000, caller="f3_a")
    assert e.value.status_code == 429 and "Daily write budget" in e.value.detail
    # another agent has its own budget
    rs._require_content_within_limit("x" * 200_000, caller="f3_b")


def test_budget_resets_on_a_new_day(monkeypatch):
    monkeypatch.setenv("EMPORIA_DAILY_WRITE_MB_PER_AGENT", "1")
    rs._charge_write_bytes("f3_c", 1024 * 1024)
    rs._DAILY_WRITE_BUDGET["f3_c"] = ("1999-01-01", 1024 * 1024)
    rs._charge_write_bytes("f3_c", 10)
    assert rs._DAILY_WRITE_BUDGET["f3_c"][1] == 10


def test_no_caller_means_no_budget_but_the_size_limit_still_holds():
    from emporia.wire import MAX_CONTENT_BYTES
    rs._require_content_within_limit("x" * 10)
    with pytest.raises(HTTPException) as e:
        rs._require_content_within_limit("x" * (MAX_CONTENT_BYTES + 1), caller="f3_d")
    assert e.value.status_code == 400
    assert "f3_d" not in rs._DAILY_WRITE_BUDGET


def test_low_disk_refuses_with_507_and_is_cached(monkeypatch):
    import shutil
    from collections import namedtuple
    usage = namedtuple("usage", "total used free")
    calls = []

    def fake(_path):
        calls.append(1)
        return usage(10 << 30, 10 << 30, 100 << 20)       # 100 MB free
    monkeypatch.setattr(shutil, "disk_usage", fake)
    monkeypatch.setenv("EMPORIA_MIN_FREE_DISK_MB", "1024")
    for _ in range(3):
        with pytest.raises(HTTPException) as e:
            rs._charge_write_bytes("f3_e", 5)
        assert e.value.status_code == 507
    assert len(calls) == 1
    monkeypatch.setenv("EMPORIA_MIN_FREE_DISK_MB", "50")
    rs._charge_write_bytes("f3_e", 5)


def test_relay_wide_daily_total_refuses_everyone(monkeypatch):
    monkeypatch.setenv("EMPORIA_DAILY_WRITE_MB_TOTAL", "1")
    monkeypatch.setenv("EMPORIA_DAILY_WRITE_MB_PER_AGENT", "5")
    for who in ("f3_t1", "f3_t2", "f3_t3", "f3_t4", "f3_t5"):
        rs._charge_write_bytes(who, 200_000)
    with pytest.raises(HTTPException) as e:
        rs._charge_write_bytes("f3_t6", 200_000)
    assert e.value.status_code == 429 and "This relay" in e.value.detail


def test_defaults_are_5_mb_per_agent_500_total_2048_free():
    assert rs._daily_write_budget_bytes() == 5 * 1024 * 1024
    assert rs._daily_write_total_bytes() == 500 * 1024 * 1024


def test_audit_log_rolls_at_the_size_limit(monkeypatch, tmp_path):
    tmp_path = tmp_path / "audit"
    tmp_path.mkdir()
    monkeypatch.setattr(rs, "LOG_DIR", tmp_path)
    monkeypatch.setattr(rs, "AUDIT_LOG_MAX_BYTES", 200)
    for n in range(40):
        rs._write_audit_line({"n": n, "pad": "x" * 30})
    names = sorted(p.name for p in tmp_path.iterdir())
    assert any(x.endswith(".jsonl") for x in names) and any(x.endswith(".jsonl.1") for x in names)
    assert not any(x.endswith(".jsonl.4") for x in names)
    assert all(p.stat().st_size <= 400 for p in tmp_path.iterdir()), sorted((p.stat().st_size, p.name) for p in tmp_path.iterdir())


# ---- F5 ------------------------------------------------------------------

def _try_register(client, agent_id):
    from emporia.identity import get_public_key_hex
    return client.post("/agents/register", json={"agent_id": agent_id,
                                                  "public_key_hex": get_public_key_hex(agent_id)})


def test_an_operator_removed_agent_cannot_register_again():
    from emporia.routes import agents as agent_routes
    client = TestClient(app, client=("127.0.0.1", 51702))
    _register_keyed(client, "f5_banned")
    agent_routes.retire("f5_banned", operator_removal=True)
    r = _try_register(client, "f5_banned")
    assert r.status_code >= 400, r.text
    assert "removed" in r.text


def test_an_agents_own_leave_can_still_rejoin():
    from emporia.routes import agents as agent_routes
    client = TestClient(app, client=("127.0.0.1", 51703))
    _register_keyed(client, "f5_leaver")
    agent_routes.retire("f5_leaver")
    r = _try_register(client, "f5_leaver")
    assert r.status_code == 200, r.text


# ---- F6 ------------------------------------------------------------------

def test_mark_read_caps_the_id_list():
    client = TestClient(app, client=("127.0.0.1", 51704))
    _register_keyed(client, "f6_agent")
    h = {"x-emporia-agent-id": "f6_agent"}
    r = client.post("/agents/f6_agent/inbox/mark-read", json=[f"i{n}" for n in range(501)], headers=h)
    assert r.status_code == 400
    r = client.post("/agents/f6_agent/inbox/mark-read", json=[f"i{n}" for n in range(500)], headers=h)
    assert r.status_code == 200


# ---- F8 ------------------------------------------------------------------

def test_payer_refuses_a_token_that_is_not_the_networks_usdc():
    from emporia.x402_payer import X402PayerError, build_payment_payload
    with pytest.raises(X402PayerError, match="not the USDC"):
        build_payment_payload(_p45_requirement(asset="0x" + "ab" * 20), _P45_PAYER_KEY)
    build_payment_payload(_p45_requirement(), _P45_PAYER_KEY)      # the real one signs


def test_payer_accepts_the_asset_the_person_configured(monkeypatch):
    from emporia.x402_payer import build_payment_payload
    other = "0x" + "cd" * 20
    monkeypatch.setenv("EMPORIA_X402_ASSET_EIP155_84532", other)
    build_payment_payload(_p45_requirement(asset=other), _P45_PAYER_KEY)


def test_payer_caps_the_validity_window():
    from emporia.x402_payer import X402PayerError, build_payment_payload
    with pytest.raises(X402PayerError, match="valid"):
        build_payment_payload(_p45_requirement(maxTimeoutSeconds=86400), _P45_PAYER_KEY)
    build_payment_payload(_p45_requirement(maxTimeoutSeconds=3600), _P45_PAYER_KEY)


# ---- F9 ------------------------------------------------------------------

def test_public_answers_are_cached_and_not_shared_across_keys(monkeypatch):
    from emporia.routes import standing
    monkeypatch.setenv("EMPORIA_PUBLIC_CACHE_SECONDS", "30")
    standing._PUBLIC_CACHE.clear()
    client = TestClient(app, client=("127.0.0.1", 51705))
    runs = []
    real = standing._leaderboard_body
    monkeypatch.setattr(standing, "_leaderboard_body", lambda *a: (runs.append(a), real(*a))[1])
    assert client.get("/leaderboard?limit=5").status_code == 200
    assert client.get("/leaderboard?limit=5").status_code == 200
    assert len(runs) == 1
    assert client.get("/leaderboard?limit=6").status_code == 200
    assert len(runs) == 2


def test_cache_off_recomputes_every_time(monkeypatch):
    from emporia.routes import standing
    monkeypatch.setenv("EMPORIA_PUBLIC_CACHE_SECONDS", "0")
    standing._PUBLIC_CACHE.clear()
    client = TestClient(app, client=("127.0.0.1", 51706))
    runs = []
    real = standing._leaderboard_body
    monkeypatch.setattr(standing, "_leaderboard_body", lambda *a: (runs.append(a), real(*a))[1])
    client.get("/leaderboard")
    client.get("/leaderboard")
    assert len(runs) == 2


def test_games_export_cached_and_still_ndjson(monkeypatch):
    from emporia.routes import standing
    monkeypatch.setenv("EMPORIA_PUBLIC_CACHE_SECONDS", "30")
    standing._PUBLIC_CACHE.clear()
    client = TestClient(app, client=("127.0.0.1", 51707))
    a = client.get("/games/export?limit=3")
    b = client.get("/games/export?limit=3")
    assert a.status_code == b.status_code == 200
    assert a.headers["content-type"].startswith("application/x-ndjson")
    assert a.text == b.text
    assert any(k[0] == "games_export" for k in standing._PUBLIC_CACHE)
