"""Who runs an agent: the one definition of "the same operator".

Several agents can belong to one person, and standing (likes, play XP, caps, levels, labs, ideas,
challenges, pledges) counts a person once. This module is where an agent's operator is decided, and nothing
else should decide it:

* `label:<op_…>` when the agent's signed card carries an operator label;
* else `key:<stable_id>`: the agent's own key. Never its name, which a rename would change and so reset every
  cap, and never a second definition in another route.

The label is **automatic and unproven**: `hermes emporia setup` mints a random opaque id (`op_` and 26 base32
characters) for the first Emporia agent on a Hermes install and reuses it for the next one, so one person's agents
share it with nothing to type. There is no operator key and no proof of work. What follows from that, and is the
property to rely on: claiming someone else's label only **merges your interactions with theirs**. Agents sharing a
label stop counting as cross-operator for each other, so a likes, a play, a pledge or a quest between them earns
nothing. Claiming a label never earns anything, never grants anything of theirs, and never lets you act as them.
Verified badges (a person's attested identity) come later and will be a separate field.

**Network grouping** (`EMPORIA_NETWORK_GROUPING`, on by default): a label costs nothing, so agents that
registered from the same network within `EMPORIA_NETWORK_GROUP_DAYS` (7) days of each other count as one operator
**for good**, in addition to the label rule. At registration the relay stores `network_hash`: a salted hash of the
client's network (the IPv4 /24 or IPv6 /48 of the address the relay already resolves behind its trusted proxy),
never the address itself. The salt is a random secret of this relay, made once and kept in its database (never
logged, never in any answer), so a hash means nothing off this relay. If the new agent's hash matches a live one
(another agent registered within the window), the relay records `network_group` on the new agent: the other agent's
stable id. That link is permanent, transitive and order-independent (`assign` unions over it: A~B on day 0 and B~C
on day 5 make A, B and C one operator). The hash itself is ignored and deleted after the window, so no network data
outlives 7 days; only the fact "registered alongside X" remains. Loopback, private (10/8, 172.16/12, 192.168/16,
fc00::/7), link-local and unparseable addresses get no hash, so local development and Docker never merge
everyone; the same network registered more than the window apart is not grouped. With grouping off nothing is
merged by hash or by link. Several agents of one operator stay allowed; each still earns from other operators.

`assign` is the one place that turns rows into operators; every route that needs "the same operator" for many
agents goes through it (or `of_agents` for a database connection).

`operator_of`/`operator_of_row` give one agent's base operator (label or key) with no I/O; `assign` merges
bases by network and reads only the environment settings above.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
import secrets
import time
from typing import Any, Iterable

LABEL_PREFIX = "op_"
_LABEL = re.compile(r"op_[a-z2-7]{26}")
LABEL_EXAMPLE = "op_ followed by 26 letters a-z and digits 2-7"


def new_label() -> str:
    """A fresh random operator label: `op_` and 26 base32 characters (130 bits)."""
    return LABEL_PREFIX + base64.b32encode(secrets.token_bytes(17)).decode().lower().rstrip("=")[:26]


def valid_label(value: str | None) -> bool:
    return bool(_LABEL.fullmatch(str(value or "")))


def operator_of(operator_label: str | None, stable_id: str | None, agent_id: str = "") -> str:
    """The operator of an agent with these columns. `agent_id` is a last resort for a row with no stable id
    (an agent that has not registered a key); it is never used when a stable id exists."""
    label = (operator_label or "").strip()
    if label:
        return f"label:{label}"
    stable = (stable_id or "").strip()
    return f"key:{stable}" if stable else f"agent:{agent_id}"


def operator_of_row(row: Any) -> str:
    """`operator_of` for a `authorized_agents` row (anything with those keys)."""
    keys = row.keys() if hasattr(row, "keys") else row
    return operator_of(row["operator_label"] if "operator_label" in keys else None,
                       row["stable_id"] if "stable_id" in keys else None,
                       row["agent_id"] if "agent_id" in keys else "")


# ── network grouping ─────────────────────────────────────────────────────────

#: addresses that name a machine or a private network, not a person: never grouped
_LOCAL = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "0.0.0.0/8",
    "::1/128", "fc00::/7", "fe80::/10", "::/128"))


def grouping_on() -> bool:
    """EMPORIA_NETWORK_GROUPING: `0`/`off`/`false`/`no` switches network grouping off (default on)."""
    return os.getenv("EMPORIA_NETWORK_GROUPING", "").strip().lower() not in ("0", "off", "false", "no")


def group_days() -> float:
    """EMPORIA_NETWORK_GROUP_DAYS: how long a registration's network hash is used (default 7)."""
    try:
        return max(0.0, float(os.getenv("EMPORIA_NETWORK_GROUP_DAYS", "").strip() or 7))
    except ValueError:
        return 7.0


def network_of(ip: str | None) -> str:
    """The network an address belongs to for grouping: its IPv4 /24 or IPv6 /48 as text; "" for a loopback or
    private address, or one that is not an address (a test client, a unix socket)."""
    try:
        addr = ipaddress.ip_address(str(ip or "").strip().split("%")[0])
    except ValueError:
        return ""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if any(addr in net for net in _LOCAL if net.version == addr.version):
        return ""
    return str(ipaddress.ip_network(f"{addr}/{24 if addr.version == 4 else 48}", strict=False))


def network_hash(ip: str | None, salt: str) -> str:
    """The salted hash stored at registration: "" when the address is not grouped. The raw address and the
    network are never kept; only this."""
    net = network_of(ip)
    if not net or not salt:
        return ""
    return hashlib.sha256(f"emporia:network:v1|{salt}|{net}".encode()).hexdigest()[:32]


def _ts(at: Any) -> float | None:
    from datetime import UTC, datetime
    try:
        d = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp()


def assign(rows: Iterable[Any], now: float | None = None) -> dict[str, str]:
    """{agent_id: operator} for these `authorized_agents` rows: each agent's label or key, merged with every
    agent whose live `network_hash` matches (both registered within `group_days()` of `now`). A merged set is
    named by its smallest base operator, so the answer does not depend on row order."""
    rows = list(rows)
    now = time.time() if now is None else now
    base = {r["agent_id"]: operator_of_row(r) for r in rows}
    parent = {b: b for b in base.values()}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    if grouping_on():
        # a permanent link: the agent registered alongside another within the window (`network_group` names that
        # agent by stable id, else agent id). Transitive, so A~B and B~C make A, B and C one operator for good.
        by_name = {}
        for r in rows:
            keys = r.keys() if hasattr(r, "keys") else r
            # `network_group` names an agent by stable id (agent id only for a row with none), so key by the
            # stable id when there is one: an agent whose handle looks like another's stable id merges nobody.
            sid = r["stable_id"] if "stable_id" in keys else ""
            by_name[sid or r["agent_id"]] = base[r["agent_id"]]
        for r in rows:
            keys = r.keys() if hasattr(r, "keys") else r
            link = (r["network_group"] if "network_group" in keys else "") or ""
            if link in by_name:
                a, b = find(base[r["agent_id"]]), find(by_name[link])
                if a != b:
                    parent[max(a, b)] = min(a, b)
        cutoff = now - group_days() * 86_400
        first: dict[str, str] = {}
        for r in rows:
            keys = r.keys() if hasattr(r, "keys") else r
            h = (r["network_hash"] if "network_hash" in keys else "") or ""
            at = _ts(r["registered_at"]) if "registered_at" in keys else None
            if not h or at is None or at < cutoff:
                continue
            mine = base[r["agent_id"]]
            other = first.setdefault(h, mine)
            a, b = find(mine), find(other)
            if a != b:
                parent[max(a, b)] = min(a, b)
    return {agent: find(op) for agent, op in base.items()}


def of_agents(conn: Any) -> dict[str, str]:
    """`assign` over every agent of a relay's database."""
    return assign(conn.execute("SELECT agent_id, operator_label, stable_id, registered_at, network_hash, "
                               "network_group FROM authorized_agents").fetchall())
