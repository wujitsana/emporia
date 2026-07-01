import { Chess } from "chess.js";
import { STARTING_FEN } from "./fen";
import type { Session, SessionAction } from "./api";

export type ChessReplayData = {
  fens: string[];
  /** SAN per applied move (length = move count). */
  sans: string[];
  moveAgents: string[];
  playable: boolean;
};

export function chessSides(session: Session): { white: string; black: string } {
  const p = session.participants ?? [];
  return { white: p[0] ?? "white", black: p[1] ?? "black" };
}

export function shortAgent(id: string, max = 14): string {
  if (id.length <= max) return id;
  return `…${id.slice(-max + 1)}`;
}

/** Rebuild positions from UCI (works even when relay stored duplicate FENs without python-chess). */
export function buildChessReplay(
  actions: SessionAction[],
  fallbackFen?: string | null,
): ChessReplayData {
  const chess = new Chess();
  const fens: string[] = [chess.fen()];
  const sans: string[] = [];
  const moveAgents: string[] = [];
  let applied = 0;

  for (const a of actions) {
    if (a.action_type !== "move") continue;
    const uci = String(a.payload?.uci ?? a.payload?.move ?? "").trim();
    if (uci.length < 4) continue;
    const from = uci.slice(0, 2);
    const to = uci.slice(2, 4);
    const promotion = uci.length > 4 ? uci[4] : undefined;
    let ok = false;
    try {
      const m = chess.move({ from, to, promotion });
      if (m) {
        sans.push(m.san);
        moveAgents.push(a.agent_id);
        fens.push(chess.fen());
        applied++;
        ok = true;
      }
    } catch {
      ok = false;
    }
    if (ok) continue;
    const serverFen = a.result?.new_state?.board_fen as string | undefined;
    if (serverFen && serverFen !== fens[fens.length - 1]) {
      try {
        chess.load(serverFen);
        sans.push(uci);
        moveAgents.push(a.agent_id);
        fens.push(serverFen);
        applied++;
      } catch {
        /* skip bad frame */
      }
    }
  }

  const playable = applied > 0 && new Set(fens).size > 1;
  if (!playable && fallbackFen && fallbackFen !== STARTING_FEN) {
    try {
      chess.load(fallbackFen);
      fens.push(fallbackFen);
    } catch {
      /* ignore */
    }
  }

  return { fens, sans, moveAgents, playable };
}

/** @deprecated use buildChessReplay */
export function fensFromActions(actions: SessionAction[], fallbackFen?: string | null): string[] {
  return buildChessReplay(actions, fallbackFen).fens;
}

export function sessionIsActive(status: string): boolean {
  return status === "active";
}

export function chessSessionPlayable(s: Session): boolean {
  if (!s.module_type.includes("chess")) return true;
  return (s.step_number ?? 0) > 0;
}