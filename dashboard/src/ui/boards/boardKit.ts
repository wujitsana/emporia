/**
 * Shared pieces of the game boards (G3; G4 and G5 build on them).
 *
 * Rules every board in this folder keeps (plan section 3.3):
 * - Visibility only: no button, no request, nothing signed. A board takes a plain view model as props
 *   and draws it; the Arena step-through and the Games panel pass the same props.
 * - Hidden stays hidden: a sealed move arrives as `state: "sealed"` with no value, and a board never
 *   draws a value for a seat that is not `revealed`. A board also drops a value it was given for a
 *   seat that is not revealed, so a caller's slip cannot put a sealed move in the DOM.
 * - Accessible: the drawing is `aria-hidden` and a real table (visually hidden, `BoardTable`) states
 *   every number the drawing shows. Nothing is told by colour alone: a mark, a pattern or a word.
 * - Colours come only from the theme variables (`--theme-*`, `--anim-*`), so every theme works.
 *   Motion only under `prefers-reduced-motion: no-preference`; no board animates today.
 * - A wide board scrolls inside its own box, never the page.
 */

export type SeatState = "waiting" | "sealed" | "revealed" | "out";

/** One player at a board. `name` is already the short display name. */
export interface BoardSeat {
  id: string;
  name: string;
  state: SeatState;
  winner?: boolean;
  /** "called 70 %", "called · sealed": shown beside the name when present. */
  note?: string;
}

/** What a sealed seat is called in the text alternative: a state, never a guess at the move. */
export const STATE_WORD: Record<SeatState, string> = {
  waiting: "no move yet",
  sealed: "sealed",
  revealed: "revealed",
  out: "out — did not reveal",
};

/** `n` things with a plural: `plural(2, "pick")` is "2 picks". */
export const plural = (n: number, word: string): string => `${n} ${word}${n === 1 ? "" : "s"}`;

/** A revealed pick on a number board. `winner` is the seat the relay's verdict names. */
export interface BoardPick {
  id: string;
  name: string;
  value: number;
  winner?: boolean;
}
