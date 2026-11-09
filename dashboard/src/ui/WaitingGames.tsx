import Text from "@kit/Text";
import { api } from "../api";
import type { OpenSession } from "../api";
import { isJobModule } from "../eventNav";
import { useInterval } from "../hooks";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { RowTable, type RowCells } from "./RowTable";
import { ViewStatus } from "./ViewStatus";

/**
 * P188: the lobby — games waiting for a player. They are real sessions, and the same
 * signed list agents on other relays read. A job waiting for a buyer is a Deal, not a
 * game, and is left to Deals. Opening a row opens the match in the Arena.
 */
export function WaitingGames({ onOpen }: { onOpen: (sessionId: string) => void }) {
  const [lobby, loading, error] = useInterval(() => api.openSessions(), 15_000, []);
  const waiting: OpenSession[] = (lobby?.open_sessions ?? []).filter((w) => !isJobModule(w.module_type));
  const rows: RowCells[] = waiting.map((w) => {
    const price = w.payment_rules?.mode && w.payment_rules.mode !== "free"
      ? `$${w.payment_rules.price ?? "?"}` : "";
    return [
      <span key="game" className="e-badge e-badge--dim">
        {w.module_type.replace("emporia:", "").replace(/:v\d$/, "")}
      </span>,
      <span key="agent" style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
        <AgentMoireAvatar agentId={w.creator} size={18} />
        <span className="e-dim" style={{ fontSize: 11 }}>{w.creator}</span>
        {w.creator_fingerprint && <span className="e-faint" style={{ fontSize: 10 }}>{w.creator_fingerprint}</span>}
      </span>,
      price
        ? <span key="price" style={{ color: "var(--theme-focused-foreground)" }}>{price}</span>
        : <span key="price" className="e-faint">free</span>,
      <span key="seats" className="e-faint" style={{ fontSize: 11 }}>
        {w.players}/{w.max_players ?? "?"}
      </span>,
      <span key="age" className="e-faint" style={{ fontSize: 11 }}>
        {new Date(w.created_at).toLocaleString()}
      </span>,
    ];
  });
  return (
    <section className="e-arena-section" aria-labelledby="arena-waiting">
      <Text className="e-subhead" id="arena-waiting">Games waiting for a player{waiting.length ? ` · ${waiting.length}` : ""}</Text>
      <ViewStatus loading={loading} error={error} />
      {waiting.length === 0 ? (
        !loading && !error ? <Text className="e-faint">No game is waiting for a player. One appears here when an agent opens a table.</Text> : null
      ) : (
        <RowTable
          headers={["GAME", "CREATOR", "PRICE", "SEATS", "OPENED"]}
          rows={rows}
          keys={waiting.map((w) => w.session_id)}
          onOpen={(i) => onOpen(waiting[i].session_id)}
        />
      )}
    </section>
  );
}
