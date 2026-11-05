/**
 * The Desktop status-bar chip: "✶ your turn" when one of the agent's games is
 * waiting on its move, else its unread inbox. Polled, because a status-bar
 * item lives for the whole window and a plugin socket may not exist; the poll
 * runs only while the chip is mounted, and stops with the plugin. Mount it with
 * `key={profile}`, so a profile switch starts from nothing.
 *
 * It sits in Desktop's chrome, outside the page's scoped CSS, so it is styled
 * with Desktop's own theme variables.
 */
import { useEffect, useRef, useState } from "react";
import type { Session } from "../api";
import { attentionLabel } from "../attention";
import { CHIP_POLL_MS, fetchAttention, type Attention } from "./attentionFetch";

export interface ChipProps {
  profile: string;
  /** Open the page on a route (`#/messages`, `#/games/<id>`). */
  open: (hash: string) => void;
  /** Called with each game that became the agent's turn since the last poll. */
  onNewTurn?: (s: Session) => void;
  pollMs?: number;
}

export function AttentionChip({ profile, open, onNewTurn, pollMs = CHIP_POLL_MS }: ChipProps) {
  const [state, setState] = useState<Attention | null>(null);
  const turnRef = useRef(onNewTurn);
  useEffect(() => { turnRef.current = onNewTurn; });

  useEffect(() => {
    const lifetime = new AbortController();
    // The first answer is the baseline: only a turn that arrives later is news.
    const seen = new Set<string>();
    const answers = { count: 0 };
    const poll = () => fetchAttention(profile).then((a) => {
      if (lifetime.signal.aborted) return;
      setState(a);
      for (const s of a?.turns ?? []) {
        if (answers.count > 0 && !seen.has(s.session_id)) turnRef.current?.(s);
        seen.add(s.session_id);
      }
      answers.count += 1;
    }, () => { if (!lifetime.signal.aborted) setState(null); });
    void poll();
    const t = setInterval(poll, pollMs);
    return () => {
      lifetime.abort();
      clearInterval(t);
    };
  }, [profile, pollMs]);

  if (!state) return null;
  const { text, urgent } = attentionLabel(state.turns.length, state.unread);
  const target = state.turns[0] ? `#/games/${encodeURIComponent(state.turns[0].session_id)}`
    : state.unread > 0 ? "#/messages" : "#/overview";
  return (
    <button
      type="button"
      onClick={() => open(target)}
      title={`Emporia · ${profile}: ${text}`}
      aria-label={`Emporia: ${text}`}
      style={{
        all: "unset",
        cursor: "pointer",
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "0 6px",
        fontSize: "inherit",
        color: urgent ? "var(--ui-accent)" : "var(--ui-text-secondary)",
        fontWeight: urgent ? 600 : undefined,
      }}
    >
      <span aria-hidden>✶</span>
      <span>{urgent || state.unread > 0 ? text : "Emporia"}</span>
    </button>
  );
}
