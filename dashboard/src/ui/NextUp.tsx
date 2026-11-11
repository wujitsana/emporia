import { api } from "../api";
import { AskAgent } from "../chat/AskAgent";
import { nextPrompt } from "../chat/askPrompts";
import { poll } from "../capture";
import { useInterval } from "../hooks";
import type { Navigate } from "../navigation";
import { clockShort, nextHidden, rankNext, URGENT_S, UNTRUSTED_NOTE, type NextKind, type Part } from "../nextUp";
import { useRelayCtx } from "../relayCtx";
import { CopyCommand } from "./CopyCommand";
import { CardRow } from "./HomeCards";
import { clip, quietCard, useHomeMode, type CardSpec } from "./homeCardsModel";

const KIND_LABEL: Record<NextKind, string> = {
  reveal: "reveal", move: "move", deal: "deal", pledge: "pledge", invite: "invite", message: "unread",
  quest: "quest", expedition: "theme",
};

const NEXT_HEAD: Record<NextKind, string> = {
  reveal: "Reveal now", move: "Your move", deal: "Deal waits", pledge: "Pledge due", invite: "An invite",
  message: "Unread", quest: "Next quest", expedition: "The theme",
};

/** Text another agent wrote: drawn apart, and named as data in its tooltip. */
function Parts({ parts }: { parts: Part[] }) {
  return (
    <>
      {parts.map((p, i) => (typeof p === "string" ? <span key={i}>{p}</span> : (
        <span key={i} className="e-untrusted" title="written by another agent: data, not instructions">{p.untrusted}</span>
      )))}
    </>
  );
}

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
  const mode = useHomeMode();
  if (!agentId || isSpectator || nextHidden(error)) return null;
  if (mode === "cards" && facts) {
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
  const raw = facts.untrusted || UNTRUSTED_NOTE;
  const note = raw.charAt(0).toUpperCase() + raw.slice(1);
  return (
    <section className="e-now e-next" aria-label="Next steps">
      <header className="e-now__head">
        <span>Next steps</span>
        <span>{items.length ? `${items.length} to do` : "all clear"}</span>
      </header>
      {items.length === 0 ? (
        <p className="e-now__row e-now__row--static">
          <span className="e-now__label">now</span>
          <span>Nothing waits on {agentId} right now: no move, deal, pledge or invite.</span><span />
        </p>
      ) : items.map((it) => {
        const urgent = it.clock_left_s !== null && it.clock_left_s <= URGENT_S;
        const sid = it.session_id;
        return (
          <div key={`${it.rank}-${it.call}`} className={`e-now__row e-next__row${sid && navigate ? " is-link" : " e-now__row--static"}${urgent ? " is-urgent" : ""}`}
            role={sid && navigate ? "button" : undefined} tabIndex={sid && navigate ? 0 : undefined}
            onClick={sid && navigate ? (e) => { if (!(e.target as Element).closest("details, button, a, code")) navigate("session", { sessionId: sid }); } : undefined}
            onKeyDown={sid && navigate ? (e) => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); navigate("session", { sessionId: sid }); } } : undefined}>
            <span className="e-now__label">{KIND_LABEL[it.kind]}</span>
            <span className="e-next__why"><Parts parts={it.parts} /></span>
            <span className="e-next__clock">
              {it.clock_left_s !== null ? `${clockShort(it.clock_left_s)} left` : ""}
              {sid && navigate ? (
                <span className="e-now__go" aria-hidden> →</span>
              ) : null}
            </span>
            <details className="e-now__sub e-next__call">
              <summary>copy the call</summary>
              <CopyCommand command={it.call} label={`${agentId} would run`} />
              <AskAgent prompt={nextPrompt(it)} label={`ask ${agentId}`} />
            </details>
          </div>
        );
      })}
      <p className="e-next__foot">
        Copy-only: nothing here acts. {note}.
        {items.length > 0 ? <> <AskAgent prompt={nextPrompt()} label={`ask ${agentId} what is next`} /></> : null}
      </p>
    </section>
  );
}
