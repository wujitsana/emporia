import type { Session } from "./api";
import { SET_PLAY_NAMES, gameBase, isSetPlay, phaseLine, setPlayView } from "./setPlay";
import { chanceLine, chanceView } from "./chance";
import { GAME_TYPES } from "./gameTypes";

export const gameName = (moduleType: string) =>
  SET_PLAY_NAMES[moduleType] ?? GAME_TYPES.find((g) => gameBase(g.id) === gameBase(moduleType))?.label
  ?? moduleType.replace("emporia:", "").replace(/:v\d+$/, "");

/** Where a live match is, in a few words: round and phase for a set play, bids for dice, steps otherwise. */
export function progressLine(s: Session): string {
  if (s.status === "waiting") return `waiting for players (${s.participants.length})`;
  if (isSetPlay(s.module_type)) {
    const v = setPlayView(s);
    const round = v.blotto && v.blotto.rounds > 1 ? `round ${v.blotto.round} of ${v.blotto.rounds} · ` : "";
    return `${round}${phaseLine(v)}`;
  }
  const c = chanceView(s);
  if (c) return chanceLine(c, s);
  return `${s.step_number} step${s.step_number === 1 ? "" : "s"}`;
}

