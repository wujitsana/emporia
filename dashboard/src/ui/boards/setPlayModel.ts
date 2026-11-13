import type { SetPlayBoardFacts, Slot } from "../../arena";
import type { SetPlayView, Seat, Verdict } from "../../setPlay";
import { fieldWinnersOf } from "../../setPlay";
import type { SeatState } from "./boardKit";

/**
 * The board of one set play at one moment, drawn from a neutral model so the Games panel (a live
 * `SetPlayView`) and the Arena step-through (a `Frame`) show the same thing. Nothing sealed is in the
 * model: a seat that has not revealed carries no value.
 */
export interface SetPlayBoardModel {
  kind: "blotto" | "two-thirds" | "lowest-unique";
  facts: SetPlayBoardFacts;
  seats: { id: string; name: string; state: SeatState; value?: number | number[]; winner?: boolean; note?: string }[];
  fieldWinners: (string | null)[] | null;
  verdict: Verdict | null;
  round?: number;
  rounds?: number;
  roundWins?: Record<string, number>;
}

const SEAT_STATE: Record<Seat["state"], SeatState> = { to_commit: "waiting", committed: "sealed", revealed: "revealed", out: "out" };

/** The Games panel's model: a live `SetPlayView`. */
export function modelFromView(v: SetPlayView, callNote: (agent: string) => string | null): SetPlayBoardModel {
  const b = v.blotto;
  const p = v.pick;
  const last = v.phase === "done" && b ? b.rounds_done[b.rounds_done.length - 1] : undefined;
  const moves = v.seats.map((s) => s.move);
  const both = v.kind === "blotto" && v.seats.length === 2 && v.seats.every((s) => s.state === "revealed" && Array.isArray(s.move));
  return {
    kind: v.kind,
    facts: {
      battlefields: b?.battlefields ?? 0, troops: b?.troops ?? 0, weights: b?.weights ?? null,
      min: p?.min ?? 0, max: p?.max ?? 0, average: p?.average ?? null, target: p?.target ?? null,
    },
    seats: v.seats.map((s) => ({
      id: s.agent, name: s.agent, state: SEAT_STATE[s.state], winner: s.winner, note: callNote(s.agent) ?? undefined,
      value: s.state === "revealed" ? s.move : undefined,
    })),
    fieldWinners: last ? last.fieldWinners
      : both ? fieldWinnersOf(moves[0] as number[], moves[1] as number[], v.seats[0].agent, v.seats[1].agent) : null,
    verdict: v.verdict,
    round: b?.round, rounds: b?.rounds, roundWins: b?.wins,
  };
}

/** The Arena's model: the frame at one step. `name` is the display name of a player id. */
export function modelFromFrame(
  f: { kind: SetPlayBoardModel["kind"]; slots: Slot[]; fields: (string | null)[] | null; verdict: Verdict | null; board: SetPlayBoardFacts; round: number; rounds: number },
  name: (id: string) => string,
): SetPlayBoardModel {
  return {
    kind: f.kind,
    facts: f.board,
    seats: f.slots.map((s) => ({
      id: s.agent, name: name(s.agent), state: s.state === "revealed" ? "revealed" : s.state === "sealed" ? "sealed" : "waiting",
      winner: f.verdict?.winner === s.agent, note: s.call,
      value: s.state === "revealed" ? s.move : undefined,
    })),
    fieldWinners: f.fields, verdict: f.verdict, round: f.round, rounds: f.rounds,
  };
}

