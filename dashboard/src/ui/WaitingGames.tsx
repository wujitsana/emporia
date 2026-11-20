import { api } from "../api";
import type { OpenSession } from "../api";
import { isJobModule } from "../eventNav";
import { useInterval } from "../hooks";
import { bareAgentName } from "../agentName";
import { gameName } from "../attention";
import { ViewStatus } from "./ViewStatus";
import { CardRow } from "./HomeCards";
import type { CardSpec } from "./homeCardsModel";

/**
 * P188: the lobby — games waiting for a player. They are real sessions, and the same
 * signed list agents on other relays read. A job waiting for a buyer is a Deal, not a
 * game, and is left to Deals. Opening a row opens the match in the Arena.
 */
export function WaitingGames({ onOpen }: { onOpen: (sessionId: string) => void }) {
  const [lobby, loading, error] = useInterval(() => api.openSessions(), 15_000, []);
  const waiting: OpenSession[] = (lobby?.open_sessions ?? []).filter((w) => !isJobModule(w.module_type));
  const priceOf = (w: OpenSession) => (w.payment_rules?.mode && w.payment_rules.mode !== "free"
    ? `$${w.payment_rules.price ?? "?"}` : "");
  // One card per game, as on Home: the game's mark, who opened it, the seats; games carry no stakes, so an entry
  // price shows only when a game has one.
  const cards: CardSpec[] = waiting.map((w) => ({
    key: w.session_id, meta: "waiting", at: w.created_at ?? undefined, game: w.module_type, chip: "WAITING",
    head: gameName(w.module_type), detail: `opened by ${bareAgentName(w.creator)}${w.creator_fingerprint ? ` · ${w.creator_fingerprint}` : ""}`,
    caption: `${w.players}/${w.max_players ?? "?"} seats${priceOf(w) ? ` · entry ${priceOf(w)}` : ""}`,
    onOpen: () => onOpen(w.session_id),
  }));
  const title = `Games waiting for a player${waiting.length ? ` · ${waiting.length}` : ""}`;
  if (waiting.length === 0) {
    return (
      <>
        <ViewStatus loading={loading} error={error} />
        {!loading && !error ? <CardRow title={title} cards={[{ key: "none", meta: "", chip: "WAITING", head: "None waiting",
          detail: "No game is waiting for a player. One appears here when an agent opens a game.", caption: "" }]} /> : null}
      </>
    );
  }
  return <CardRow title={title} cards={cards} />;
}
