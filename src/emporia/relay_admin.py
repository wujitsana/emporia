"""What a relay's operator sees of their relay (P179): its settings and its activity.

Served to the relay operator only (`GET /operator/overview`), for the dashboard's
Settings view ("This relay") and its Relay view. Two parts:

* **settings** — every setting a relay runner changes, its value, what it is
  for, ✓/!/✗, and how to change it (an `emporia` command, or the variable to
  set in the relay's .env before a restart). Secrets appear only as "set" or
  "not set".
* **stats** — aggregates: counts and sums, totals and the last 30 days by day.
  Never a message, a deliverable or an address: DMs and private sessions are
  sealed anyway, and an operator's analytics need nothing anyone wrote.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

_RESTART = "set it in the relay's .env, then restart the relay"


def _item(group: str, label: str, value: Any, about: str, command: str = "",
          level: str = "ok") -> dict[str, Any]:
    return {"group": group, "label": label, "value": str(value), "about": about,
            "command": command, "level": level, "person_only": False}


def _on(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _is_set(name: str) -> str:
    return "set" if os.getenv(name, "").strip() else "not set"


def settings() -> list[dict[str, Any]]:
    """The relay's settings, as its operator would change them."""
    from emporia import relay_access
    from emporia import relay_server as rs
    from emporia.networks import is_testnet, label
    from emporia.payments import _x402_enabled, _x402_facilitator_url, _x402_networks

    nets = _x402_networks() if _x402_enabled() else []
    tempo = (rs._tempo_cfg() or {}).get("network")
    escrow = rs._escrow_network()
    live = [n for n in [*nets, *([tempo] if tempo else [])] if not is_testnet(n)]
    stripe = rs._stripe_key_mode() or "off"
    guard = rs.guardrails.DEFAULT_MODE
    out = [
        _item("Identity", "Operator agent", rs._relay_operator_id() or "not set",
              "The agent that may see this page, payment records and operator actions.",
              f"EMPORIA_AGENT_ID — {_RESTART}", "ok" if rs._relay_operator_id() else "warn"),
        _item("Identity", "Relay id", rs.RELAY_ID if os.getenv("EMPORIA_RELAY_ID") else "random each start",
              "Its name to other relays. Set one before linking relays.", f"EMPORIA_RELAY_ID — {_RESTART}",
              "ok" if os.getenv("EMPORIA_RELAY_ID") or not rs.FEDERATED_RELAYS else "warn"),
        _item("Identity", "Public address", os.getenv("EMPORIA_DASHBOARD_BASE_URL") or "not set",
              "Its https address, behind a TLS proxy, for anyone off this machine.",
              f"EMPORIA_DASHBOARD_BASE_URL — {_RESTART} (README § Deployment)"),
        _item("Access", "Who may use it", f"{relay_access.mode()} — {len(relay_access.members())} member(s)",
              "open: any agent. read-only: anyone browses, members act. closed: members only, no federation.",
              "emporia relay access open|read-only|closed · emporia relay members add NAME|KEY",
              "warn" if relay_access.mode() == "open" and live else "ok"),
        _item("Access", "Nous sign-in to register", "required" if rs.REQUIRE_NOUS else "not required",
              "Recommended for a public relay: one verified account per agent seat.",
              f"EMPORIA_REQUIRE_NOUS=1 — {_RESTART}"),
        _item("Access", "Writes need Nous", "yes" if rs.WRITE_REQUIRES_NOUS else "no",
              "Unverified agents can read but not act.", f"EMPORIA_WRITE_REQUIRES_NOUS=1 — {_RESTART}"),
        _item("Access", "Proof of key at registration", "required" if rs.REQUIRE_CHALLENGE else "OFF",
              "An agent proves it holds its key before it registers.",
              f"EMPORIA_REQUIRE_CHALLENGE=1 — {_RESTART}", "ok" if rs.REQUIRE_CHALLENGE else "danger"),
        _item("Access", "Guardrails", guard, "Checks on moves and messages (enforce, audit, off).",
              f"EMPORIA_GUARDRAILS_MODE=enforce — {_RESTART}", "ok" if guard == "enforce" else "warn"),
        _item("Access", "Writes per agent per minute", os.getenv("RATE_LIMIT_PER_MINUTE", "60"),
              "Each agent's write budget.", f"RATE_LIMIT_PER_MINUTE — {_RESTART}"),
        _item("Access", "Trust local requests without sign-in", "yes" if _on("EMPORIA_TRUST_LOCAL_HEADER_AUTH") else "no",
              "Only for a relay nobody else can reach.", f"EMPORIA_TRUST_LOCAL_HEADER_AUTH=0 — {_RESTART}",
              "warn" if _on("EMPORIA_TRUST_LOCAL_HEADER_AUTH") else "ok"),
        _item("Fees", "Your fee", f"{rs.OPERATOR_FEE_BPS / 100:.2f}%",
              "Taken from each pot or payment, never more than offered.", f"OPERATOR_FEE_BPS — {_RESTART}"),
        _item("Fees", "Fee receiver (USDC)", os.getenv("EMPORIA_ESCROW_FEE_RECEIVER") or "not set",
              "The address your USDC fees are paid to.", "emporia setup relay --fee-receiver 0x…",
              "ok" if os.getenv("EMPORIA_ESCROW_FEE_RECEIVER") or not escrow else "warn"),
        _item("USDC", "Networks", ", ".join(label(n) for n in nets) or "off",
              "Where agents pay USDC (x402)." + (" Includes mainnet: real money." if live else ""),
              "emporia setup relay [--with solana-devnet,tempo-moderato]", "warn" if live else "ok"),
        _item("USDC", "Facilitator", _x402_facilitator_url() or "not set",
              "Settles USDC payments on chain.", f"EMPORIA_X402_FACILITATOR_URL — {_RESTART}"),
        _item("USDC", "Escrow holds", label(escrow) if escrow else "off",
              "Holds that pay only the payee or go back to the payer; you never hold the money.",
              "emporia setup relay"),
        _item("USDC", "Operator key", _is_set("EMPORIA_OPERATOR_PRIVATE_KEY") if os.getenv(
              "EMPORIA_OPERATOR_SIGNER", "env") == "env" else os.getenv("EMPORIA_OPERATOR_SIGNER"),
              "Signs escrow captures and voids; never a spending key.", "emporia setup relay"),
        _item("Tempo", "Tempo", label(tempo) if tempo else "off",
              "Stablecoin on Tempo, straight to the payee.", f"EMPORIA_TEMPO_NETWORK — {_RESTART}"),
        _item("Cards", "Stripe", stripe, "Card payments, paid to each payee's Stripe account.",
              "emporia setup stripe"),
        _item("Cards", "Stripe webhook secret", _is_set("STRIPE_WEBHOOK_SECRET"),
              "Required with a live key.", "emporia setup stripe",
              "danger" if stripe == "live" and not os.getenv("STRIPE_WEBHOOK_SECRET") else "ok"),
        _item("Cards", "Card money held by you", "ON" if rs.custodial_cards_allowed() else "off",
              "Off by default: flows that leave money on your Stripe balance.",
              f"EMPORIA_CUSTODIAL_CARDS=0 — {_RESTART}", "warn" if rs.custodial_cards_allowed() else "ok"),
        _item("Cards", "Card stakes on games", "ON" if rs.card_game_stakes_allowed() else "off",
              "Off by default: Stripe restricts games of skill for money.",
              f"EMPORIA_CARD_GAME_STAKES=0 — {_RESTART}", "warn" if rs.card_game_stakes_allowed() else "ok"),
        _item("Games", "Games", ", ".join(sorted(rs.MODULE_REGISTRY)),
              "The games and session types this relay hosts.",
              f"EMPORIA_MODULES (extra modules, after review) — {_RESTART}"),
        _item("Games", "Stakes on engine-decided games", "ON" if _on("EMPORIA_ENGINE_GAME_STAKES") else "off",
              "Off by default: an engine wins chess, and nothing here can tell.",
              f"EMPORIA_ENGINE_GAME_STAKES=0 — {_RESTART}", "warn" if _on("EMPORIA_ENGINE_GAME_STAKES") else "ok"),
        _item("Games", "Idle games end after", f"{os.getenv('EMPORIA_SESSION_IDLE_HOURS', '24')} h",
              "A game nobody moves in is abandoned and stakes go back.", f"EMPORIA_SESSION_IDLE_HOURS — {_RESTART}"),
        _item("Federation", "DMs with other relays", "on" if os.getenv("EMPORIA_FEDERATED_DMS", "") == "1" else "off",
              "Sealed DMs to and from agents on linked relays; each agent still opts in to receive them.",
              f"EMPORIA_FEDERATED_DMS=1 — {_RESTART}"),
        _item("Federation", "Linked relays", ", ".join(rs.FEDERATED_RELAYS) or "none (standalone)",
              "Relays that share listings with this one; each links the other.",
              "emporia relay link|unlink URL · emporia relay links"),
        _item("Operations", "Error tracking", "on" if os.getenv("SENTRY_DSN") else "off",
              "Sentry, if you use it.", f"SENTRY_DSN — {_RESTART}"),
        _item("Operations", "Log level", os.getenv("EMPORIA_LOG_LEVEL", "INFO"), "The relay's own logs.",
              f"EMPORIA_LOG_LEVEL — {_RESTART}"),
        _item("Operations", "Checks", "", "✓ / ! / ✗ for the relay and every agent on this machine.",
              "emporia doctor"),
    ]
    return out


def _one(conn, sql: str, args: tuple = ()) -> int:
    row = conn.execute(sql, args).fetchone()
    return int((row[0] if row else 0) or 0)


def stats(conn, now: datetime | None = None) -> dict[str, Any]:
    """Counts and sums only. `conn` is the relay's database."""
    now = now or datetime.now(UTC)
    d1, d7, d30 = ((now - timedelta(days=n)).isoformat() for n in (1, 7, 30))
    active = ("SELECT COUNT(DISTINCT a) FROM ("
              "SELECT agent_id a, created_at t FROM actions UNION ALL "
              "SELECT sender_id, created_at FROM room_messages UNION ALL "
              "SELECT sender_id, created_at FROM dm_messages) WHERE t >= ?")
    out: dict[str, Any] = {
        "agents": {
            "registered": _one(conn, "SELECT COUNT(*) FROM authorized_agents WHERE is_active = 1"),
            "nous_verified": _one(conn, "SELECT COUNT(*) FROM authorized_agents WHERE is_active = 1 "
                                        "AND trust_level = 'nous_verified'"),
            "with_wallet": _one(conn, "SELECT COUNT(*) FROM authorized_agents WHERE is_active = 1 "
                                      "AND x402_pay_to_json NOT IN ('', '{}')"),
            "new_7d": _one(conn, "SELECT COUNT(*) FROM authorized_agents WHERE registered_at >= ?", (d7,)),
            "active_24h": _one(conn, active, (d1,)),
            "active_7d": _one(conn, active, (d7,)),
        },
        "sessions": {
            "by_status": {r[0]: r[1] for r in conn.execute(
                "SELECT status, COUNT(*) FROM sessions GROUP BY status ORDER BY 2 DESC")},
            "by_kind": [{"module_type": r[0], "total": r[1], "last_7d": r[2]} for r in conn.execute(
                "SELECT module_type, COUNT(*), SUM(created_at >= ?) FROM sessions "
                "GROUP BY module_type ORDER BY 2 DESC LIMIT 12", (d7,))],
            "created_7d": _one(conn, "SELECT COUNT(*) FROM sessions WHERE created_at >= ?", (d7,)),
        },
        "community": {
            "rooms": _one(conn, "SELECT COUNT(*) FROM rooms"),
            "paid_rooms": _one(conn, "SELECT COUNT(*) FROM rooms WHERE entry_fee_cents > 0"),
            "encrypted_rooms": _one(conn, "SELECT COUNT(*) FROM rooms WHERE encrypted = 1"),
            "room_messages_7d": _one(conn, "SELECT COUNT(*) FROM room_messages WHERE created_at >= ?", (d7,)),
            "dm_threads": _one(conn, "SELECT COUNT(*) FROM dm_threads"),
            "dm_messages_7d": _one(conn, "SELECT COUNT(*) FROM dm_messages WHERE created_at >= ?", (d7,)),
            "agora_topics": _one(conn, "SELECT COUNT(*) FROM agora_topics"),
            "agora_posts_7d": _one(conn, "SELECT COUNT(*) FROM agora_posts WHERE created_at >= ?", (d7,)),
            "open_listings": _one(conn, "SELECT COUNT(*) FROM listings WHERE expires_at IS NULL OR expires_at > ?",
                                  (now.isoformat(),)),
        },
        "money": {
            # Payments that arrived, by rail; fees from settlements; USDC escrow holds by state.
            "payments": [{"rail": r[0], "count": r[1], "cents": r[2] or 0, "cents_30d": r[3] or 0}
                         for r in conn.execute(
                             "SELECT rail, COUNT(*), SUM(amount_cents), SUM(CASE WHEN created_at >= ? "
                             "THEN amount_cents ELSE 0 END) FROM payments WHERE status = 'confirmed' "
                             "GROUP BY rail ORDER BY 3 DESC", (d30,))],
            "fees": [{"rail": r[0], "settlements": r[1], "volume_cents": r[2] or 0, "fee_cents": r[3] or 0,
                      "fee_cents_30d": r[4] or 0}
                     for r in conn.execute(
                         "SELECT rail, COUNT(*), SUM(total_stake_cents), SUM(platform_fee_cents), "
                         "SUM(CASE WHEN created_at >= ? THEN platform_fee_cents ELSE 0 END) "
                         "FROM settlements GROUP BY rail ORDER BY 4 DESC", (d30,))],
            "escrow_holds": [{"state": r[0], "count": r[1], "usdc": round((r[2] or 0) / 1e6, 2)}
                             for r in conn.execute(
                                 "SELECT state, COUNT(*), SUM(CAST(amount_atomic AS INTEGER)) "
                                 "FROM escrow_holds GROUP BY state ORDER BY 2 DESC")],
            "usdc_direct": [{"network": r[0], "count": r[1], "cents": r[2] or 0} for r in conn.execute(
                "SELECT network, COUNT(*), SUM(amount_cents) FROM x402_settle_intents "
                "WHERE status = 'settled' GROUP BY network ORDER BY 3 DESC")],
        },
    }
    days = [(now - timedelta(days=n)).date().isoformat() for n in range(29, -1, -1)]
    series: dict[str, dict[str, int]] = {d: {"agents": 0, "sessions": 0, "messages": 0,
                                             "payments_cents": 0, "fees_cents": 0} for d in days}
    for key, sql in (
        ("agents", "SELECT substr(registered_at, 1, 10), COUNT(*) FROM authorized_agents "
                   "WHERE registered_at >= ? GROUP BY 1"),
        ("sessions", "SELECT substr(created_at, 1, 10), COUNT(*) FROM sessions WHERE created_at >= ? GROUP BY 1"),
        ("messages", "SELECT substr(t, 1, 10), COUNT(*) FROM (SELECT created_at t FROM room_messages UNION ALL "
                     "SELECT created_at FROM dm_messages) WHERE t >= ? GROUP BY 1"),
        ("payments_cents", "SELECT substr(created_at, 1, 10), SUM(amount_cents) FROM payments "
                           "WHERE status = 'confirmed' AND created_at >= ? GROUP BY 1"),
        ("fees_cents", "SELECT substr(created_at, 1, 10), SUM(platform_fee_cents) FROM settlements "
                       "WHERE created_at >= ? GROUP BY 1"),
    ):
        for day, n in conn.execute(sql, (d30,)):
            if day in series:
                series[day][key] = int(n or 0)
    out["daily"] = [{"day": d, **series[d]} for d in days]
    out["generated_at"] = now.isoformat()
    return out
