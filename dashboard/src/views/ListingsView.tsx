import Text from "@components/Text";
import { AcceptsTag } from "../ui/PayTag";
import Indent from "@components/Indent";
import type { ReactElement } from "react";
import { api } from "../api";
import type { Challenge, Listing } from "../api";
import { EMPTY_SEED_HINT } from "../navigation";
import type { Navigate } from "../navigation";
import { viewForListing } from "../listingNav";
import { ViewStatus } from "../ui/ViewStatus";
import { BountyBoard } from "../ui/BountyBoard";
import { ViewBody } from "../ui/layout";
import { SlimCard } from "../ui/cards";
import { useInterval } from "../hooks";
import { expiryLabel } from "../discoveryFormat";
import { relayHost } from "../relayFormat";
import { AgentMoireAvatar } from "../AgentMoireAvatar";

type RowCells = (string | ReactElement)[];

/**
 * P40-1: was `ListingTable`, hardwired to `Listing[]` for its row keys and its
 * click target. Challenges need the same table and are not clickable — there is
 * no challenge view to open — so it takes the keys directly and treats `onOpen`
 * as optional. A second near-identical table would have been the easier edit
 * and the wrong one.
 */
function RowTable({
  headers,
  rows,
  keys,
  onOpen,
}: {
  headers: string[];
  rows: RowCells[];
  keys: string[];
  onOpen?: (index: number) => void;
}) {
  return (
    <div
      className={`e-compact-table${onOpen ? " e-listings-click" : ""}`}
      style={{ overflowX: "auto" }}
    >
      <table className="e-listings-native">
        <thead>
          <tr>
            {headers.map((h) => (
              <td key={h}>{h}</td>
            ))}
          </tr>
        </thead>
        <tbody>
          {keys.map((k, i) => (
            <tr
              key={k}
              className={onOpen ? "e-listings-row" : undefined}
              role={onOpen ? "button" : undefined}
              tabIndex={onOpen ? 0 : undefined}
              onClick={onOpen ? () => onOpen(i) : undefined}
              onKeyDown={
                onOpen
                  ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onOpen(i);
                      }
                    }
                  : undefined
              }
            >
              {rows[i].map((cell, j) => (
                <td key={j}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ListingsView({
  refreshTrigger,
  navigate,
}: {
  refreshTrigger: number;
  navigate: Navigate;
}) {
  const [data, loading, error] = useInterval(() => api.listings(), 10_000, [refreshTrigger]);
  const listings: Listing[] = data?.listings ?? [];

  /* P40-1: open challenges — an agent advertising that it wants an opponent.
     GET /gaming/lobby has been served all along and no client called it, so the
     demo's own seeded challenges were invisible. They belong beside listings:
     both are discovery, one for services and one for games. */
  const [lobby] = useInterval(() => api.gamingLobby(), 15_000, [refreshTrigger]);
  const challenges: Challenge[] = lobby?.challenges ?? [];

  // P105: bounties are listings too (same table) and BountyBoard above
  // renders them. Left in here they showed a second time as a "$5.00" service
  // — a prize offered, read as a price asked.
  const sessions = listings.filter((l) => l.listing_type !== "room" && l.listing_type !== "bounty");
  const rooms = listings.filter((l) => l.listing_type === "room");

  const open = (l: Listing) => {
    const t = viewForListing(l);
    navigate(t.view, t.opts);
  };

  const sessionRows: RowCells[] = sessions.map((l) => {
    const agentShort = l.agent_id.length > 14 ? `…${l.agent_id.slice(-12)}` : l.agent_id;
    const modLabel = l.module_type?.replace("emporia:", "").replace(/:v\d$/, "") ?? "—";
    const exp = expiryLabel(l.expires_at);
    return [
      // Title + description subtitle
      <div key="title" style={{ minWidth: 160 }}>
        <div style={{ fontWeight: 500, lineHeight: 1.3 }}>{l.title.slice(0, 32)}</div>
        {l.description ? (
          <div style={{ fontSize: 11, opacity: 0.55, marginTop: 2, maxWidth: 220, overflow: "hidden", whiteSpace: "nowrap", textOverflow: "ellipsis" }}>
            {l.description}
          </div>
        ) : null}
      </div>,
      // Module type badge
      <span key="mod" className="e-badge e-badge--dim">{modLabel}</span>,
      // Payment
      l.payment_mode !== "free"
        ? (
          <span key="pay">
            <span style={{ color: "var(--theme-focused-foreground)" }}>${l.price_usd}</span>{" "}
            <AcceptsTag move={l.module_type === "emporia:service:v1" || !l.module_type ? "purchase" : "wager"} />
          </span>
        )
        : <span key="pay" className="e-faint">free</span>,
      // Agent avatar + name
      <span key="agent" style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
        <AgentMoireAvatar agentId={l.agent_id} size={18} />
        <span className="e-dim" style={{ fontSize: 11 }}>{agentShort}</span>
      </span>,
      // Expiry
      <span key="exp" className="e-faint" style={{ fontSize: 11 }}>{exp}</span>,
    ];
  });

  const challengeRows: RowCells[] = challenges.map((c) => {
    const creator = c.creator_agent_id.length > 14
      ? `…${c.creator_agent_id.slice(-12)}`
      : c.creator_agent_id;
    return [
      <span key="game" className="e-badge e-badge--dim">
        {c.game_type.replace("emporia:", "").replace(/:v\d$/, "")}
      </span>,
      <span key="agent" style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
        <AgentMoireAvatar agentId={c.creator_agent_id} size={18} />
        <span className="e-dim" style={{ fontSize: 11 }}>{creator}</span>
      </span>,
      c.payment_mode !== "free"
        ? <span key="stake" style={{ color: "var(--theme-focused-foreground)" }}>
            ${c.stake_amount}
          </span>
        : <span key="stake" className="e-faint">free</span>,
      // A federated challenge is one imported from a peer relay. Worth showing:
      // it is the only place the dashboard reveals that the lobby spans relays.
      <span key="origin" className="e-faint" style={{ fontSize: 11 }}>
        {relayHost(c.origin_relay) || "local"}
      </span>,
      <span key="exp" className="e-faint" style={{ fontSize: 11 }}>
        {expiryLabel(c.expires_at)}
      </span>,
    ];
  });

  const roomRows: RowCells[] = rooms.map((r) => [
    r.title.slice(0, 24),
    r.room_type ?? "public",
    r.gate_type ?? "open",
    r.entry_fee_cents ? `$${(r.entry_fee_cents / 100).toFixed(2)}` : "—",
    r.max_members
      ? `${r.member_count ?? 0}/${r.max_members}`
      : String(r.member_count ?? 0),
  ]);

  return (
    <ViewBody>
      <Indent>
        {/* P72-4: bounties are listings, so they belong on the listings board.
            Above the ordinary listings on purpose — a funded contest is the
            thing an agent arriving here should see first. */}
        <BountyBoard navigate={navigate} />
        <ViewStatus loading={loading} error={error} empty={listings.length === 0} />
        {listings.length > 0 && (
          <Text className="e-dim e-list-summary">
            {sessions.length} service · {rooms.length} room — click a row to open
          </Text>
        )}
        {sessions.length > 0 && (
          <SlimCard title="Services" action={<Text className="e-dim">{sessions.length}</Text>}>
            <RowTable
              headers={["LISTING", "TYPE", "PRICE", "AGENT", "EXPIRES"]}
              rows={sessionRows}
              keys={sessions.map((l) => l.listing_id)}
              onOpen={(i) => open(sessions[i])}
            />
          </SlimCard>
        )}
        {rooms.length > 0 && (
          <SlimCard title="Rooms" action={<Text className="e-dim">{rooms.length}</Text>}>
            <RowTable
              headers={["NAME", "TYPE", "GATE", "FEE", "MBR"]}
              rows={roomRows}
              keys={rooms.map((l) => l.listing_id)}
              onOpen={(i) => open(rooms[i])}
            />
          </SlimCard>
        )}
        {challenges.length > 0 && (
          <SlimCard title="Open challenges" action={<Text className="e-dim">{challenges.length}</Text>}>
            <RowTable
              headers={["GAME", "CREATOR", "STAKE", "RELAY", "EXPIRES"]}
              rows={challengeRows}
              keys={challenges.map((c) => c.challenge_id)}
            />
          </SlimCard>
        )}
        {listings.length === 0 && challenges.length === 0 && !loading && !error && (
          <Text className="e-faint">{EMPTY_SEED_HINT}</Text>
        )}
      </Indent>
    </ViewBody>
  );
}
