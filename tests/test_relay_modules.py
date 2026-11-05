"""P291: relay modules — what a relay runs beyond its core, said once, checked at start."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from emporia import cli, relay_modules as rm
from emporia import relay_server as rs

_SWITCHES = ("EMPORIA_TRANSPARENCY_LOG", "EMPORIA_GAMIFY", "EMPORIA_FEDERATED_DMS", "EMPORIA_X402_ENABLED",
             "EMPORIA_ESCROW_NETWORK", "EMPORIA_TEMPO_NETWORK", "STRIPE_SECRET_KEY", "EMPORIA_CUSTODIAL_CARDS",
             "EMPORIA_CARD_GAME_STAKES", "EMPORIA_RELAY_ACCESS")


@pytest.fixture
def core(monkeypatch):
    """A relay running its core only."""
    for name in _SWITCHES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMPORIA_RELAY_ACCESS", "open")
    return monkeypatch


def test_the_core_alone_runs_no_module(core):
    assert rm.running() == {} and rm.advertise() == {} and rm.problems() == []


def test_each_module_is_on_when_its_settings_say_so(core):
    core.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    core.setenv("EMPORIA_GAMIFY", "sheets,bugs")
    core.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    ads = rm.advertise(["https://peer.example"])
    assert ads["log"] == {"v": 1, "parts": []}
    assert ads["gamify"]["parts"] == ["bugs", "sheets"] and ads["gamify"]["formulas"]
    assert ads["money"]["parts"] == ["cards"] and ads["money"]["cards"] == "test"
    assert ads["federation"] == {"v": 1, "parts": [], "links": ["https://peer.example"]}
    assert rm.describe(ads) == "money (cards) · log · federation · gamify (bugs, sheets)"


def test_a_part_without_its_dependency_is_off_and_the_relay_refuses_to_start(core):
    core.setenv("EMPORIA_GAMIFY", "bugs")
    assert "gamify" not in rm.running()                 # its only part is off, so the module is
    assert rm.off_because("gamify", "bugs") == "the gamify module's bugs part needs the log module, which this relay does not run"
    [why] = rm.problems()
    assert why.startswith("gamify: bugs needs the log module") and "emporia setup log" in why
    with pytest.raises(rs.RelayModulesWrong, match="bugs needs the log"):
        rs._check_relay_modules()
    core.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    assert rm.on("gamify", "bugs") and rm.problems() == []
    rs._check_relay_modules()


def test_half_set_up_money_and_federation_are_refused_by_name(core):
    core.setenv("EMPORIA_X402_ENABLED", "1")            # no facilitator, no network
    core.setenv("EMPORIA_CARD_GAME_STAKES", "1")        # no Stripe key
    core.setenv("EMPORIA_FEDERATED_DMS", "1")           # nothing linked: fine, federation just isn't running
    core.setenv("EMPORIA_GAMIFY", "sheets,raids")
    wrong = "\n".join(rm.problems())
    for said in ("EMPORIA_X402_ENABLED is on", "EMPORIA_CARD_GAME_STAKES is on", "no part called raids"):
        assert said in wrong
    assert "money" not in rm.running() and "no relay is linked" not in wrong


def test_a_closed_relay_does_not_federate(core):
    core.setenv("EMPORIA_RELAY_ACCESS", "closed")
    assert "federation" not in rm.running(["https://peer.example"])


def test_card_and_health_say_it_in_one_field(core):
    core.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    client = TestClient(rs.app, client=("127.0.0.1", 51900))
    card, health = client.get("/relay/card").json(), client.get("/health").json()
    assert card["version"] == 2 and card["relay_modules"] == health["relay_modules"]
    assert set(card["relay_modules"]) == {"log"}
    for gone in ("transparency_log", "gamify", "formulas", "money", "links"):
        assert gone not in card, gone
    assert "transparency_log" not in health
    assert "emporia:chess:v1" in card["games"]          # games are not relay modules


def test_agents_see_only_the_tools_their_relays_serve():
    every = rm.all_tools()
    assert rm.tools_to_hide([{}]) == every                                  # the core only
    assert rm.tools_to_hide([{}, None]) == set()                            # one did not answer: hide nothing
    assert rm.tools_to_hide([]) == set()
    sheets = rm.tools_to_hide([{"gamify": {"parts": ["sheets"]}}])
    assert "character_sheet" not in sheets and "report_bug" in sheets
    # a module's tools show when any relay it uses runs it — home or a visit
    both = rm.tools_to_hide([{"log": {"parts": []}}, {"money": {"parts": ["x402", "escrow"]}}])
    assert not both & {"check_relay_log", "register_x402_wallet", "reclaim_escrow"}
    assert {"create_payment_intent", "report_bug"} <= both


def test_every_tool_a_module_names_is_a_real_tool():
    from emporia import mcp_server
    tools = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert rm.all_tools() <= tools, rm.all_tools() - tools


def test_the_mcp_server_hides_and_shows_them(monkeypatch):
    from emporia import mcp_server

    class Answer:
        status_code = 200

        def __init__(self, mods):
            self.mods = mods

        def json(self):
            return {"relay_modules": self.mods}

    def names():
        return {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}

    monkeypatch.setattr(mcp_server, "_visits", lambda: [])
    monkeypatch.setattr(mcp_server.httpx, "get", lambda url, timeout=5: Answer({"gamify": {"parts": ["sheets"]}}))
    try:
        hidden = mcp_server._fit_tools_to_relays()
        assert "report_bug" in hidden and "report_bug" not in names() and "character_sheet" in names()
        monkeypatch.setattr(mcp_server, "_visits", lambda: ["https://other.example"])
        monkeypatch.setattr(mcp_server.fetch, "get", lambda url, timeout=5: Answer({"gamify": {"parts": ["bugs"]}, "log": {}}))
        mcp_server._fit_tools_to_relays()
        assert "report_bug" in names()                                      # the visited relay runs bug quests
    finally:
        mcp_server.mcp.enable(names=rm.all_tools())


def test_relay_modules_command_writes_only_what_changes(tmp_path, monkeypatch):
    op = tmp_path / "op"
    op.mkdir()
    (op / ".env").write_text("EMPORIA_GAMIFY=sheets\n")
    monkeypatch.setattr(cli, "this_profile", lambda: op)
    monkeypatch.setattr(cli, "health", lambda timeout=3.0: None)
    assert cli.main(["relay", "modules", "--modules", "gamify,log", "--yes"]) == 0
    text = (op / ".env").read_text()
    assert "EMPORIA_TRANSPARENCY_LOG=on" in text and "EMPORIA_GAMIFY=sheets\n" in text
    assert cli.main(["relay", "modules", "--modules", "nonsense", "--yes"]) == 1
    # off again: the switch goes off, nothing else is touched
    assert cli.main(["relay", "modules", "--modules", "log", "--yes"]) == 0
    text = (op / ".env").read_text()
    assert "EMPORIA_GAMIFY=\n" in text and "EMPORIA_TRANSPARENCY_LOG=on" in text


def test_bug_quests_bring_the_log_or_stay_off(tmp_path, monkeypatch):
    op = tmp_path / "op"
    op.mkdir()
    (op / ".env").write_text("EMPORIA_GAMIFY=sheets,bugs\n")
    monkeypatch.setattr(cli, "this_profile", lambda: op)
    monkeypatch.setattr(cli, "health", lambda timeout=3.0: None)
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setattr(cli, "ask", lambda q, default: False)                # "no, not the log"
    assert cli.main(["relay", "modules", "--modules", "gamify", "--yes"]) == 0
    assert "EMPORIA_GAMIFY=sheets\n" in (op / ".env").read_text()


# ── P291: linked relays take each other's modules into account ──────────────

PEER = "https://peer-p291.example"


def _peer_card(mods, *, signer=None, pinned=True):
    from cryptography.hazmat.primitives.asymmetric import ed25519

    from emporia.identity import relay_card_bytes
    key = signer or ed25519.Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes_raw().hex()
    card = {"kind": "emporia-relay-card", "version": 2, "relay_id": "peer", "relay_public_key": pub,
            "relay_key_history": [], "relay_modules": mods}
    card["signature"] = key.sign(relay_card_bytes(card)).hex()
    if pinned:
        rs._FEDERATION_PEER_PUBKEYS[PEER] = pub
    return card


class _Answer:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


@pytest.fixture
def peer(monkeypatch):
    from emporia import fetch
    served = {}

    async def request(method, url, **kw):
        return _Answer(served["card"])
    monkeypatch.setattr(fetch, "request", request)
    yield served
    rs._FEDERATION_PEER_PUBKEYS.pop(PEER, None)
    rs._FEDERATION_PEER_MODULES.pop(PEER, None)
    rs._FEDERATION_LAST_SYNC.pop(PEER, None)


def _federated_listing(listing_id, mode):
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO listings (listing_id, title, agent_id, payment_mode, price_usd, "
                     "origin_relay, created_at, federated_from) VALUES (?,?,?,?,?,?,?,?)",
                     (listing_id, "t", "seller", mode, "5" if mode != "free" else "0", PEER, "2026-11-04", PEER))


def _listing_ids():
    with rs.DB_LOCK, rs.get_db() as conn:
        return {r[0] for r in conn.execute("SELECT listing_id FROM listings WHERE federated_from = ?", (PEER,))}


def test_unknown_until_its_signed_card_says(peer):
    import anyio
    assert rs.peer_runs(PEER, "money") is None
    peer["card"] = _peer_card({"log": {"v": 1, "parts": []}}, pinned=False)      # nothing pinned for it
    assert anyio.run(rs._pull_peer_modules, PEER) is None and rs.peer_runs(PEER, "log") is None
    peer["card"] = _peer_card({"log": {"v": 1, "parts": []}})
    peer["card"]["relay_modules"] = {"money": {"v": 1, "parts": ["x402"]}}       # changed after signing
    assert anyio.run(rs._pull_peer_modules, PEER) is None and rs.peer_runs(PEER, "money") is None
    peer["card"] = _peer_card({"log": {"v": 1, "parts": []}})
    assert anyio.run(rs._pull_peer_modules, PEER) == {"log": {"v": 1, "parts": []}}
    assert rs.peer_runs(PEER, "log") is True and rs.peer_runs(PEER, "money") is False


def test_paid_listings_from_a_relay_that_takes_no_payments_go(peer):
    import anyio
    _federated_listing("p291-free", "free")
    _federated_listing("p291-paid", "x402")
    peer["card"] = _peer_card({"log": {"v": 1, "parts": []}})
    anyio.run(rs._pull_peer_modules, PEER)
    assert _listing_ids() >= {"p291-free"} and "p291-paid" not in _listing_ids()
    assert rs._FEDERATION_LAST_SYNC[PEER]["listings_unpayable"] == 1
    _federated_listing("p291-paid", "x402")
    peer["card"] = _peer_card({"money": {"v": 1, "parts": ["x402"]}})
    anyio.run(rs._pull_peer_modules, PEER)
    assert "p291-paid" in _listing_ids()


def test_whether_it_links_back_is_recorded(peer, monkeypatch):
    import anyio
    monkeypatch.setattr(rs, "RELAY_BASE_URL", "https://me-p291.example")
    peer["card"] = _peer_card({"federation": {"v": 1, "parts": ["dms"], "links": ["https://me-p291.example/"]}})
    anyio.run(rs._pull_peer_modules, PEER)
    assert rs._FEDERATION_LAST_SYNC[PEER]["links_back"] is True
    peer["card"] = _peer_card({"federation": {"v": 1, "parts": [], "links": ["https://else.example"]}})
    anyio.run(rs._pull_peer_modules, PEER)
    assert rs._FEDERATION_LAST_SYNC[PEER]["links_back"] is False


def test_no_sealed_dm_to_a_relay_whose_card_says_it_takes_none(peer):
    import anyio
    from fastapi import HTTPException

    from emporia import federation_dm as fd
    fd.refuse_unless_peer_takes_dms(PEER)                                    # not known: as before
    peer["card"] = _peer_card({"federation": {"v": 1, "parts": [], "links": []}})
    anyio.run(rs._pull_peer_modules, PEER)
    with pytest.raises(HTTPException) as e:
        fd.refuse_unless_peer_takes_dms(PEER)
    assert e.value.status_code == 409 and "does not exchange DMs" in e.value.detail
    peer["card"] = _peer_card({"federation": {"v": 1, "parts": ["dms"], "links": []}})
    anyio.run(rs._pull_peer_modules, PEER)
    fd.refuse_unless_peer_takes_dms(PEER)


def test_a_relay_that_stops_its_log_is_said_to_have(peer, monkeypatch):
    from emporia import logcheck
    monkeypatch.setattr(logcheck, "load", lambda name=None: {"relays": {PEER: {"state": {"size": 3}}}})
    rs._note_peer_log_off(PEER)
    assert rs._FEDERATION_LAST_SYNC[PEER]["log"].startswith("stopped")
    monkeypatch.setattr(logcheck, "load", lambda name=None: {})
    rs._note_peer_log_off(PEER)
    assert rs._FEDERATION_LAST_SYNC[PEER]["log"] == "not run there"


def test_add_and_remove_modules_by_name(tmp_path, monkeypatch):
    op = tmp_path / "op"
    op.mkdir()
    (op / ".env").write_text("FEDERATED_RELAYS=https://peer.example\n")
    monkeypatch.setattr(cli, "this_profile", lambda: op)
    monkeypatch.setattr(cli, "health", lambda timeout=3.0: None)

    def env():
        return dict(line.split("=", 1) for line in (op / ".env").read_text().splitlines() if "=" in line)
    assert cli.main(["relay", "modules", "add", "log", "gamify:sheets,bugs", "--yes"]) == 0
    assert env()["EMPORIA_TRANSPARENCY_LOG"] == "on" and env()["EMPORIA_GAMIFY"] == "sheets,bugs"
    assert env()["FEDERATED_RELAYS"] == "https://peer.example", "what was not named is kept"
    assert cli.main(["relay", "modules", "remove", "gamify:bugs", "--yes"]) == 0
    assert env()["EMPORIA_GAMIFY"] == "sheets" and env()["EMPORIA_TRANSPARENCY_LOG"] == "on"
    assert cli.main(["relay", "modules", "add", "federation:dms", "--yes"]) == 0
    assert env()["EMPORIA_FEDERATED_DMS"] == "1"
    assert cli.main(["relay", "modules", "remove", "federation:dms", "--yes"]) == 0
    assert env()["EMPORIA_FEDERATED_DMS"] == "0" and env()["FEDERATED_RELAYS"] == "https://peer.example"
    assert cli.main(["relay", "modules", "remove", "gamify", "--yes"]) == 0
    assert env()["EMPORIA_GAMIFY"] == ""
    # refused, nothing written: a missing dependency, a module or part that does not exist, money's parts
    before = (op / ".env").read_text()
    assert cli.main(["relay", "modules", "remove", "log", "--yes"]) == 0          # nothing needs it now
    (op / ".env").write_text(before)
    assert cli.main(["relay", "modules", "add", "gamify:bugs", "--yes"]) == 0    # the log is on
    assert cli.main(["relay", "modules", "remove", "log", "--yes"]) == 1         # bug quests need it
    for bad in (["add", "chat"], ["add", "gamify:raids"], ["add", "money:cards"], ["add"], ["drop", "log"]):
        assert cli.main(["relay", "modules", *bad, "--yes"]) == 1, bad
    assert "EMPORIA_TRANSPARENCY_LOG=on" in (op / ".env").read_text()
