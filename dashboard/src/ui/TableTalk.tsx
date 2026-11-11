import { useEffect, useMemo, useRef } from "react";
import type { GameChatMessage } from "../api";
import { playerName } from "../arena";
import { isNarrow } from "./overlayFocus";
import { SaidLine } from "./GameTalk";

/** What `useSaid` returns: the lines said at the table, opened where this machine can. */
export interface Talk { messages: GameChatMessage[]; opened: Record<string, string>; enabled: boolean; failed: boolean }

/** Every line with the step it sits at (said with a move, or placed by its time), in the order it was said. */
function placed(said: Map<number, GameChatMessage[]>): { step: number; m: GameChatMessage }[] {
  const out: { step: number; m: GameChatMessage }[] = [];
  for (const [step, list] of said) for (const m of list) out.push({ step, m });
  return out.sort((a, b) => a.step - b.step || a.m.created_at.localeCompare(b.m.created_at));
}

/**
 * A game's table talk, always on the page beside the board or replay (not only the line of the move on
 * screen). Each line says which move it came with; a click seeks the replay there, and the lines of the
 * step on screen are marked. Drawn only when the game has chat. Read-only.
 */
export function TableTalk({ talk, said, book, at, onSeek }: {
  talk: Talk; said: Map<number, GameChatMessage[]>; book?: Record<string, string>; at: number; onSeek?: (step: number) => void;
}) {
  const lines = useMemo(() => placed(said), [said]);
  const listRef = useRef<HTMLUListElement>(null);
  // Follow the replay inside the panel (the page itself does not scroll).
  useEffect(() => {
    const list = listRef.current;
    const now = list?.querySelector<HTMLElement>(".is-now");
    if (list && now) list.scrollTop = now.offsetTop - list.offsetTop - list.clientHeight / 2 + now.clientHeight / 2;
  }, [at, lines.length]);
  if (talk.failed) {
    return <section className="e-talk" aria-label="Table talk"><p className="e-faint">Table talk could not be read. It may be private to the players.</p></section>;
  }
  if (!talk.enabled) return null;
  return (
    <section className="e-talk" aria-label="Table talk">
      <details open={!isNarrow()}>
        <summary className="e-talk__head">TABLE TALK · {lines.length}</summary>
        {lines.length === 0 ? <p className="e-faint">Nobody has spoken.</p> : (
          <ul ref={listRef} className="e-talk__list">
            {lines.map(({ step, m }) => (
              <li key={m.message_id}>
                <button type="button" className={`e-talk__line${step === at ? " is-now" : ""}`} aria-current={step === at ? "true" : undefined}
                  disabled={!onSeek} onClick={() => onSeek?.(step)}>
                  <span className="e-faint e-talk__who">{step === 0 ? "before the first move" : `move ${step}`} · {playerName(m.agent_id, book, 14)}</span>
                  <SaidLine m={m} opened={talk.opened} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </details>
    </section>
  );
}
