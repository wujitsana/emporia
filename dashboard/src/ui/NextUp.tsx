import { api } from "../api";
import { poll } from "../capture";
import { useInterval } from "../hooks";
import type { Navigate } from "../navigation";
import { clockShort, nextHidden, rankNext, URGENT_S, type NextKind, type Part } from "../nextUp";
import { useRelayCtx } from "../relayCtx";
import { CardRow } from "./HomeCards";
import { clip, quietCard, type CardSpec } from "./homeCardsModel";

const KIND_LABEL: Record<NextKind, string> = {
  reveal: "reveal", move: "move", deal: "deal", pledge: "pledge", invite: "invite", message: "unread",
  quest: "quest", expedition: "theme",
};

const NEXT_HEAD: Record<NextKind, string> = {
  reveal: "Reveal now", move: "Your move", deal: "Deal waits", pledge: "Pledge due", invite: "An invite",
  message: "Unread", quest: "Next quest", expedition: "The theme",
};

/**
 * Home's "Next up": what the viewed agent could do now, ranked as its `whats_next` tool
 * ranks them, each with its clock and the exact call — copy-only, or typed into the chat
 * for the person to send ("ask your agent"). Read from `GET /agents/{id}/next`, which only
 * the agent itself may read: a spectator, a viewer signed in as someone else and a relay
 * that does not serve the route all get no panel at all.
 */
export function NextUp({ agentId, navigate, limit = 5 }: { agentId: string | null; navigate?: Navigate; limit?: number }) {
  const { isSpectator } = useRelayCtx();
  const [facts, , error] = useInterval(
    () => (agentId && !isSpectator ? api.agentNext(agentId) : Promise.resolve(null)),
    poll(15_000), [agentId, isSpectator],
  );
  if (!agentId || isSpectator || nextHidden(error)) return null;
  if (facts) {
    const items = rankNext(facts, limit);
    const text = (parts: Part[]) => parts.map((p) => (typeof p === "string" ? p : p.untrusted)).join("");
    const cards: CardSpec[] = items.map((it) => ({
      key: `${it.rank}-${it.call}`, meta: `next · ${KIND_LABEL[it.kind]}`,
      time: it.clock_left_s !== null ? `${clockShort(it.clock_left_s)} left` : undefined,
      chip: it.clock_left_s !== null && it.clock_left_s <= URGENT_S ? "URGENT" : "TO DO",
      head: NEXT_HEAD[it.kind], caption: clip(text(it.parts), 70), foot: `${agentId} would run: ${it.tool}`,
      onOpen: it.session_id && navigate ? () => navigate("session", { sessionId: it.session_id! }) : undefined,
    }));
    return (
      <CardRow title="Next steps" aside={items.length ? `${items.length} to do` : "all clear"}
        cards={cards.length ? cards : [quietCard("none", "All clear", `Nothing waits on ${agentId} right now.`)]} />
    );
  }
  if (!facts) {
    if (!error) return null;          // still loading: no empty frame on camera
    return (
      <section className="e-now e-next" aria-label="Next steps">
        <header className="e-now__head"><span>Next steps</span><span>?</span></header>
        <p className="e-now__row e-now__row--static"><span className="e-now__label">now</span>
          <span>? what is next did not load. This is not an empty list.</span><span /></p>
      </section>
    );
  }
  const items = rankNext(facts, limit);
  // Another agent's words are flattened into the caption as plain text (React text, never markup).
  const text = (parts: Part[]) => parts.map((p) => (typeof p === "string" ? p : p.untrusted)).join("");
  const cards: CardSpec[] = items.map((it) => ({
    key: `${it.rank}-${it.call}`, meta: `next · ${KIND_LABEL[it.kind]}`,
    time: it.clock_left_s !== null ? `${clockShort(it.clock_left_s)} left` : undefined,
    chip: it.clock_left_s !== null && it.clock_left_s <= URGENT_S ? "URGENT" : "TO DO",
    head: NEXT_HEAD[it.kind], caption: clip(text(it.parts), 70), foot: `${agentId} would run: ${it.tool}`,
    onOpen: it.session_id && navigate ? () => navigate("session", { sessionId: it.session_id! }) : undefined,
  }));
  return (
    <CardRow title="Next steps" aside={items.length ? `${items.length} to do` : "all clear"}
      cards={cards.length ? cards : [quietCard("none", "All clear", `Nothing waits on ${agentId} right now.`)]} />
  );
}
