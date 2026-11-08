"""Agent-neutral: whatever matters to an agent that is not Hermes, `emporia.agent_sdk.EmporiaAgent` does.

`PARITY` maps every MCP tool to the SDK method that does it, or says why none does (a person's or
operator's act, or something this release leaves to a later one). The first test holds the table to the
registered tools both ways, so a new tool cannot arrive without a decision. The rest drive the added
methods against an in-process relay, with two SDK agents of different operators and the x402 fakes.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from emporia import operators
from emporia import relay_server as rs
from emporia.agent_sdk import EmporiaAgent
from emporia.identity import get_public_key_hex

# tool -> SDK method; or None with the reason it is not in the SDK.
PERSON = "a person's or operator's act (the SDK agent's operator does it on its own side)"
LATER = "not in the SDK yet"
PARITY: dict[str, str | None] = {
    # identity, relays
    "register_agent": "register", "agent_card": "get_agent_profile", "list_agents": "list_agents",
    "get_agent_profile": "get_agent_profile", "counterparty_record": "counterparty_record",
    "start_stripe_onboarding": "start_stripe_onboarding", "supported_games": "supported_games",
    "relay_payment_info": "get_relay_descriptor", "check_relay_log": "check_relay_log",
    "scan_relays": None, "visit_relay": None, "leave_relay": None,        # federation: see below
    # feed, threads, Agora
    "post": "post", "read_feed": "read_feed", "read_thread": "read_thread", "whats_happening": "whats_happening",
    "react": "react", "follow": "follow", "delete_item": "delete_item", "open_room_from_post": "open_from_post",
    "create_agora_topic": "create_agora_topic", "list_agora_topics": "get_agora_topics",
    "subscribe_agora_topic": "subscribe_agora_topic", "unsubscribe_agora_topic": "unsubscribe_agora_topic",
    "invite_to_agora_topic": "invite_to_agora_topic", "revoke_agora_invite": "revoke_agora_invite",
    "kick_from_agora_topic": "kick_from_agora_topic",
    # rooms
    "create_room": "create_room", "list_rooms": "get_rooms", "join_room": "join_room", "leave_room": "leave_room",
    "send_room_message": "send_room_message", "get_room_messages": "get_room_messages",
    "invite_to_room": "invite_to_room", "revoke_room_invite": "revoke_room_invite",
    "kick_from_room": "kick_from_room", "verify_room_chain": "verify_room_chain", "check_room": None,
    # DMs and inbox
    "send_dm": "send_dm", "get_dm_messages": "get_dm_messages", "list_dm_threads": "list_dm_threads",
    "verify_dm_chain": "verify_dm_chain", "get_inbox": "get_inbox", "mark_inbox_read": "mark_inbox_read",
    "block_dm_sender": "block_dm_sender", "list_dm_blocks": "list_dm_blocks",
    "set_cross_relay_dms": None, "archive_now": None, "read_archive": None, "my_settings": None,
    # games and sessions
    "create_session": "create_session", "join_session": "join_session", "list_sessions": "list_sessions",
    "get_session": "get_session", "leave_session": "leave_session", "abandon_session": "abandon_session",
    "submit_action": "submit_action", "commit_move": "commit_move", "reveal_move": "reveal_move",
    "send_game_chat": "say_in_game", "get_game_chat": "get_game_chat", "reveal_models": "reveal_models",
    "get_game_models": "get_game_models", "get_model_results": "get_model_results",
    "challenge": "challenge", "list_challenges": "list_challenges", "decline_challenge": "decline_challenge",
    "game_results": "game_results", "leaderboard": "leaderboard", "export_games": "export_games",
    "verify_session": None, "validate_turn": None,
    # money
    "create_payment_intent": "create_payment_intent", "fund_session": "fund_session",
    "confirm_delivery": "confirm_delivery", "dispute_delivery": "dispute_delivery",
    "get_settlements": "get_settlements", "register_x402_wallet": "register_x402_wallet",
    "reclaim_escrow": None, "x402_wallet_info": None,
    # quests, levels, season, what next
    "my_quests": "my_quests", "character_sheet": "character_sheet", "season": "season", "season_board": "season_board",
    "whats_next": "whats_next", "my_stats": None,
    # labs
    "lab_threads": "lab_threads", "github_claim_line": "github_claim_line", "lab_report": "lab_report",
    # the card's X link
    "x_claim_line": "x_claim_line", "x_link": "x_link", "confirm_x_link": "confirm_x_link",
    # pledges
    "make_pledge": None, "attest_pledge": None, "list_pledges": None,
    "sign_dashboard_challenge": None,
}

# Why each tool left out is left out.
LEFT_OUT = {
    **dict.fromkeys(["set_cross_relay_dms", "archive_now", "read_archive", "my_settings", "my_stats",
                     "sign_dashboard_challenge", "x402_wallet_info"], PERSON),
    **dict.fromkeys(["scan_relays", "visit_relay", "leave_relay"],
                    "federation needs the operator's yes; an SDK agent reaches another relay by being given "
                    "that relay's URL (one `EmporiaAgent` per relay, its own key on each)"),
    **dict.fromkeys(["check_room", "verify_session", "reclaim_escrow", "validate_turn"],
                    "a local check or a recovery over keys the plugin pins and stores; " + LATER),
    **dict.fromkeys(["make_pledge", "attest_pledge", "list_pledges"], "pledges: " + LATER),
}


def test_every_mcp_tool_is_mapped_to_an_sdk_method_or_left_out_with_a_reason():
    import asyncio as _a

    import emporia.mcp_server as m
    tools = {t.name for t in _a.run(m.mcp.list_tools())}
    assert tools == set(PARITY), (sorted(tools - set(PARITY)), sorted(set(PARITY) - tools))
    for tool, method in PARITY.items():
        if method is None:
            assert LEFT_OUT.get(tool), f"{tool}: no reason for leaving it out"
        else:
            assert callable(getattr(EmporiaAgent, method, None)), f"{tool}: EmporiaAgent.{method} is missing"
            assert tool not in LEFT_OUT, tool
    assert len([t for t, v in PARITY.items() if v]) >= 75


def test_the_sdk_methods_are_documented():
    undocumented = [n for n, f in vars(EmporiaAgent).items()
                    if not n.startswith("_") and callable(f) and not (f.__doc__ or "").strip()]
    allowed = {"register", "create_session", "get_session", "list_sessions", "send_message", "get_messages",
               "get_rooms", "get_room", "get_relay_descriptor", "health", "get_room_messages",
               "listen_session", "listen_agent", "stream_session_events"}
    assert not [n for n in undocumented if n not in allowed], undocumented


# ── the added methods, against an in-process relay ─────────────────────────────────────────────────

def name(tag: str) -> str:
    return f"sp_{tag}_{uuid.uuid4().hex[:6]}"


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setenv("EMPORIA_DECLARE_MODEL", "on")
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    monkeypatch.setenv("EMPORIA_QUEST_OTHERS", "any")
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    monkeypatch.setenv("EMPORIA_LABS", "1")
    monkeypatch.delenv("EMPORIA_HOUSE_AGENTS", raising=False)
    return httpx.ASGITransport(app=rs.app)


def agent(transport, tag: str, **kw) -> EmporiaAgent:
    n = name(tag)
    kw.setdefault("operator_label", operators.new_label())
    kw.setdefault("client_name", "sdk")
    a = EmporiaAgent(relay_url="http://testserver", agent_id=n, public_key_hex=get_public_key_hex(n), **kw)
    a._http = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    return a


def test_posts_threads_follows_deletes_and_what_is_happening(relay):
    async def scenario():
        a, b = agent(relay, "a", model="model-a"), agent(relay, "b", model="model-b")
        await a.register()
        await b.register()
        topic = name("t").replace("_", "-")
        await a.create_agora_topic(topic, slug=topic)
        first = await a.post("First post.", topic=topic, tags=[["t", "sdk"], ["model", "forged"]])
        reply = await b.post("A reply.", topic=topic, reply_to=f"post:{first['post_id']}")
        quote = await b.post("Quoting it.", topic=topic, quote_of=f"post:{first['post_id']}")
        await b.react(f"post:{first['post_id']}", "like")
        await a.follow(b.agent_id)
        thread = await a.read_thread(f"post:{first['post_id']}")
        skim = await a.skim_thread(f"post:{first['post_id']}")
        feed = await b.read_feed(topic=topic)
        happening = await a.whats_happening()
        gone = await b.delete_item("post", reply["post_id"])
        return first, reply, quote, thread, skim, feed, happening, gone, a, b

    first, reply, quote, thread, skim, feed, happening, gone, a, b = asyncio.run(scenario())
    tags = {p["post_id"]: p.get("tags") for p in feed["posts"]}
    assert ["model", "model-a"] in tags[first["post_id"]] and ["model", "forged"] not in tags[first["post_id"]]
    assert any(p["post_id"] == reply["post_id"] for p in thread["posts"])
    assert skim["mode"] == "skim" and {"top", "latest", "stats"} <= set(skim)
    assert {"topics", "open_challenges", "next_since"} <= set(happening)
    assert gone["removed"]["role"] == "author" and gone["removed"]["item"] == reply["post_id"]


def test_a_sealed_dm_goes_between_two_sdk_agents_and_only_they_read_it(relay):
    async def scenario():
        a, b = agent(relay, "a"), agent(relay, "b")
        await a.register()
        await b.register()
        thread = await a.start_dm(b.agent_id, encrypted=True)
        sent = await a.send_dm(thread["thread_id"], "for your eyes only")
        got = await b.get_dm_messages(thread["thread_id"])
        threads = await b.list_dm_threads()
        return sent, got, threads

    sent, got, threads = asyncio.run(scenario())
    msgs = got["messages"] if isinstance(got, dict) else got
    assert any(m.get("decrypted") and "for your eyes only" in str(m.get("content")) for m in msgs), msgs
    assert threads


def test_quests_levels_sheet_season_labs_and_lookups(relay):
    async def scenario():
        a, b = agent(relay, "a"), agent(relay, "b")
        await a.register()
        await b.register()
        quests = await a.my_quests()
        sheet = await a.character_sheet()
        season = await a.season()
        board = await a.season_board()
        profile = await b.get_agent_profile(a.agent_id)
        record = await b.counterparty_record(a.agent_id)
        agents = await a.list_agents()
        nxt = await a.whats_next()
        labs = await a.lab_threads()
        games = await a.supported_games()
        claim = await a.github_claim_line("acme/widgets", "pr", 7, "a" * 40)
        with pytest.raises(ValueError):
            await a.github_claim_line("not a repo", "pr", 7)
        await a.block_dm_sender(b.agent_id)
        blocks = await a.list_dm_blocks()
        return quests, sheet, season, board, profile, record, agents, nxt, labs, games, claim, blocks, a

    quests, sheet, season, board, profile, record, agents, nxt, labs, games, claim, blocks, a = asyncio.run(scenario())
    assert quests["formula"] == "quests:v8" and quests["level"]["level"] >= 1 and quests["quests"]
    assert "craft" in sheet and "boards" in season and board
    assert profile["client"]["name"] == "sdk" and profile["has_operator_label"] and profile["operator_label"] is None, profile
    assert record is not None and agents and nxt and "labs" in labs and games
    assert claim["line"].startswith("Emporia:") and claim["claim"]["repo"] == "acme/widgets"
    assert blocks


# ── a paid room: two legs, the creator's share and the relay's 1%, with the x402 fakes ─────────────

_PAYER_KEY = "0x" + "11" * 32          # a throwaway key: it signs fakes; no chain is touched
_USDC_84532 = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
_FEE_ADDRESS = "0x" + "fe" * 20


@patch("emporia.payments.x402_settle_payment", new_callable=AsyncMock)
@patch("emporia.payments.x402_verify_payment", new_callable=AsyncMock)
def test_an_sdk_agent_pays_a_rooms_entry_in_two_legs_and_joins(mock_verify, mock_settle, relay, monkeypatch):
    pytest.importorskip("eth_account")
    for k, v in {
        "EMPORIA_X402_ENABLED": "1", "EMPORIA_X402_FACILITATOR_URL": "https://facilitator.example.test",
        "EMPORIA_X402_NETWORKS": "eip155:84532", "EMPORIA_X402_ASSET_EIP155_84532": _USDC_84532,
        "EMPORIA_X402_EXTRA_EIP155_84532": '{"name": "USDC", "version": "2"}',
        "EMPORIA_FEE_ADDRESS": _FEE_ADDRESS,
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("EMPORIA_FEE_BPS", raising=False)
    mock_verify.return_value = {"isValid": True}
    tag = uuid.uuid4().hex                  # the relay refuses a transaction id it already recorded: fresh ones
    mock_settle.side_effect = [{"success": True, "transaction": f"0xsdk_creator_{tag}"},
                               {"success": True, "transaction": f"0xsdk_fee_{tag}"}]
    creator_wallet = "0x" + "cd" * 20
    monkeypatch.setenv("EMPORIA_PAYOUT_ADDRESSES", creator_wallet)     # the person's list: an agent signs no other

    async def scenario():
        owner = agent(relay, "owner")
        payer = agent(relay, "payer", x402_private_key=_PAYER_KEY)
        await owner.register()
        await payer.register()
        await owner.register_x402_wallet(creator_wallet)
        room = await owner.create_room(name("paid"), room_type="private", gate_type="paid", entry_fee_cents=1000)
        room_id = (room.get("room") or room)["room_id"]
        from emporia.agent_sdk import PaymentRequiredError
        with pytest.raises(PaymentRequiredError) as asked:
            await payer.join_room(room_id, payer.agent_id)                      # no pay_with_x402: a 402, nothing signed
        try:
            joined = await payer.join_room(room_id, payer.agent_id, pay_with_x402=True)
        except PaymentRequiredError as e:
            raise AssertionError(f"the paid retry was declined: {e.detail}") from e
        await payer.send_room_message(room_id, "Paid and in.")
        left = await payer.leave_room(room_id)
        return asked.value.detail, joined, left

    detail, joined, left = asyncio.run(scenario())
    assert detail["terms"]["fee_bps"] == 100 and detail["terms"]["fee_cents"] == 10
    assert detail["terms"]["creator_cents"] == 990 and detail["terms"]["total_cents"] == 1000
    assert joined and left is not None
    paid_to = [c.args[1]["payTo"] for c in mock_settle.call_args_list]
    assert paid_to == [creator_wallet, _FEE_ADDRESS], "the creator's share to the creator, 1% to the relay's address"
