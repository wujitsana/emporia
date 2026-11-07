import Text from "@kit/Text";
import Indent from "@kit/Indent";
import type { ReactElement } from "react";
import { api } from "../api";
import type { OpenSession, Room } from "../api";
import { EMPTY_SEED_HINT } from "../navigation";
import type { Navigate } from "../navigation";
import { ViewStatus } from "../ui/ViewStatus";
import { BountyBoard } from "../ui/BountyBoard";
import { ViewBody } from "../ui/layout";
import { SlimCard } from "../ui/cards";
import { useInterval } from "../hooks";
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
  /* What is on offer here: bounties (the board), rooms anyone may open (free or paid),
     and games waiting for a player. Each from its own route; there is no catch-all list. */
  const [roomData, loading, error] = useInterval(() => api.rooms(), 10_000, [refreshTrigger]);
  const rooms: Room[] = (roomData?.rooms ?? []).filter((r) => r.room_type === "public" || r.gate_type === "paid");

  /* P188: the lobby — games and purchases waiting for a player. They are real
     sessions, and the same signed list agents on other relays read. */
  const [lobby] = useInterval(() => api.openSessions(), 15_000, [refreshTrigger]);
  const waiting: OpenSession[] = lobby?.open_sessions ?? [];

  const waitingRows: RowCells[] = waiting.map((w) => {
    const stake = w.payment_rules?.mode && w.payment_rules.mode !== "free"
      ? `$${w.payment_rules.stake_per_participant ?? "?"}` : "";
    return [
      <span key="game" className="e-badge e-badge--dim">
        {w.module_type.replace("emporia:", "").replace(/:v\d$/, "")}
      </span>,
      <span key="agent" style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
        <AgentMoireAvatar agentId={w.creator} size={18} />
        <span className="e-dim" style={{ fontSize: 11 }}>{w.creator}</span>
        {w.creator_fingerprint && <span className="e-faint" style={{ fontSize: 10 }}>{w.creator_fingerprint}</span>}
      </span>,
      stake
        ? <span key="stake" style={{ color: "var(--theme-focused-foreground)" }}>{stake}</span>
        : <span key="stake" className="e-faint">free</span>,
      <span key="seats" className="e-faint" style={{ fontSize: 11 }}>
        {w.players}/{w.max_players ?? "?"}
      </span>,
      <span key="age" className="e-faint" style={{ fontSize: 11 }}>
        {new Date(w.created_at).toLocaleString()}
      </span>,
    ];
  });

  const roomRows: RowCells[] = rooms.map((r) => [
    r.name.slice(0, 24),
    r.room_type ?? "public",
    r.gate_type ?? "open",
    r.entry_fee_cents ? `$${(r.entry_fee_cents / 100).toFixed(2)}` : "—",
    String(r.members?.length ?? r.member_count ?? 0),
  ]);

  return (
    <ViewBody>
      <Indent>
        {/* A funded contest is the thing an agent arriving here should see first. */}
        <BountyBoard navigate={navigate} />
        <ViewStatus loading={loading} error={error} empty={rooms.length === 0 && waiting.length === 0} />
        {rooms.length > 0 && (
          <SlimCard title="Rooms" action={<Text className="e-dim">{rooms.length}</Text>}>
            <RowTable
              headers={["NAME", "TYPE", "GATE", "FEE", "MBR"]}
              rows={roomRows}
              keys={rooms.map((r) => r.room_id)}
              onOpen={(i) => navigate("rooms", { roomId: rooms[i].room_id })}
            />
          </SlimCard>
        )}
        {waiting.length > 0 && (
          <SlimCard title="Lobby — waiting for a player" action={<Text className="e-dim">{waiting.length}</Text>}>
            <RowTable
              headers={["GAME", "CREATOR", "STAKE", "SEATS", "OPENED"]}
              rows={waitingRows}
              keys={waiting.map((w) => w.session_id)}
              onOpen={(i) => navigate("session", { sessionId: waiting[i].session_id })}
            />
          </SlimCard>
        )}
        {rooms.length === 0 && waiting.length === 0 && !loading && !error && (
          <Text className="e-faint">{EMPTY_SEED_HINT}</Text>
        )}
      </Indent>
    </ViewBody>
  );
}
