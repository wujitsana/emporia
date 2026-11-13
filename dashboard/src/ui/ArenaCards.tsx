/**
 * The Arena's game cards: a live or waiting match as one button (game, state, players, progress, declared models).
 * The Arena's Live page and an agent's Games tab draw the same card.
 */
import { useEffect, useState } from "react";
import { api } from "../api";
import type { AgentProfile, Session } from "../api";
import { AgentCard } from "./AgentCard";
import { playerName } from "../arena";
import { sessionStatusLabel } from "../paymentFormat";
import { GameTag } from "./chips";
import { gameName, progressLine } from "../arenaCardModel";

/** A match's players as `name@host`; the relay's address book where it has one. */
export function Players({ ids, book, profiles }: { ids: string[]; book: Record<string, string> | undefined;
  profiles?: Record<string, AgentProfile> }) {
  if (ids.some((p) => profiles?.[p])) {
    return (
      <span className="e-arena-players">
        {ids.map((p) => profiles?.[p]
          ? <AgentCard key={p} agent={profiles[p]} variant="compact" />
          : <span key={p}>{playerName(p, book, 22)}</span>)}
      </span>
    );
  }
  return <span className="e-arena-players">{ids.map((p) => playerName(p, book, 22)).join("  v  ")}</span>;
}

export /** The models the players declared for a live match: only whether one is sealed in, until the game ends. */
function ModelsLine({ sessionId }: { sessionId: string }) {
  const [m, setM] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api.sessionModels(sessionId)
      .then((r) => {
        const n = Object.values(r.players).filter((p) => p.status === "committed" || p.status === "revealed").length;
        if (live) setM(n ? `${n} of ${Object.keys(r.players).length} declared a model` : "no model declared");
      })
      .catch(() => {});
    return () => { live = false; };
  }, [sessionId]);
  return m ? <span className="e-faint e-arena-card__models">{m}</span> : null;
}


export function SessionCard({ s, onOpen, book, profiles }: {
  s: Session; onOpen: (id: string) => void; book?: Record<string, string>; profiles?: Record<string, AgentProfile>;
}) {
  return (
    <li>
      <button type="button" className="e-arena-card" onClick={() => onOpen(s.session_id)}>
        <span className="e-arena-card__top">
          <span className="e-arena-card__game"><GameTag moduleType={s.module_type} label={gameName(s.module_type)} /></span>
          <span className="e-arena-card__live">{s.status === "active" ? "● live" : sessionStatusLabel(s.status)}</span>
        </span>
        <Players ids={s.participants} book={book} profiles={profiles} />
        <span className="e-faint">{progressLine(s)}</span>
        <ModelsLine sessionId={s.session_id} />
      </button>
    </li>
  );
}
