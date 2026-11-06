/**
 * Launch additions: how the Gamify tab words what the relay computed — a report's claimed
 * severity beside the awarded one ("called it"), and a row of the improvement board.
 * Shown, never ranked: "called" is a count beside the points, not a ladder.
 */
import type { SeasonBoards } from "./api";

/** A reporter's claim beside the operator's award; the claim alone until a verdict. */
export const claimedAwarded = (claimed: string, awarded: string | null) =>
  awarded ? `claimed ${claimed} · awarded ${awarded}` : `${claimed} (claimed)`;

type BoardRow = SeasonBoards["boards"][string][number];

const count = (k: number, one: string) => `${k} ${k === 1 ? one : `${one}s`}`;

/** One row of the improvement board: its points, then what earned them, then "called it". */
export const improvementRow = (r: BoardRow) => {
  const parts = [`${r.points} pts`, count(r.accepted ?? 0, "bug")];
  if (r.adopted) parts.push(count(r.adopted, "idea"));
  if (r.accepted) parts.push(`called ${r.called ?? 0}/${r.accepted}`);
  return parts.join(" · ");
};
