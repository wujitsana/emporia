/** What Home's cards (ui/HomeCards.tsx) are made of: the view mode, a card's fields and two small helpers. */
import { createContext, useContext } from "react";
import { readPref, writePref } from "../prefs";

export type HomeMode = "list" | "cards";

const KEY = (viewer: string | null) => `emporia_home_mode:${viewer ?? ""}`;

/** The viewer's Home mode on this machine; cards unless they chose the list. */
export function readHomeMode(viewer: string | null, fallback: HomeMode = "cards"): HomeMode {
  const saved = readPref(KEY(viewer));
  return saved === "list" || saved === "cards" ? saved : fallback;
}

export function writeHomeMode(viewer: string | null, mode: HomeMode): void {
  writePref(KEY(viewer), mode);
}

export const HomeModeContext = createContext<HomeMode>("list");
export const useHomeMode = (): HomeMode => useContext(HomeModeContext);

export interface CardSpec {
  key: string;
  /** Status line, left: what and where ("game · bargain"). */
  meta: string;
  /** Status line, right: a clock ("12 min left"), when the card has no moment (`at`). */
  time?: string;
  /** Status line, right: the moment it happened (ISO): "3m ago", the exact time on hover. */
  at?: string;
  /** The agent the card is about or by: their sigil sits at the chip row's end. */
  agent?: string;
  /** The game's module type: its pixel badge sits there instead (a card about a game). */
  game?: string;
  /** A progress bar of ten cells (the level's XP). */
  bar?: { value: number; max: number };
  /** An exact call to copy, shown on the caption bar (read-only: a copy button, nothing runs). */
  copy?: string;
  /** Cards of one kind in a row merge into one "×N" card: the kind (usually the headline). */
  merge?: string;
  /** What a merged card names this one by ("liar's dice with beta"); the caption when none. */
  label?: string;
  /** The game or post this card is about: two cards of one kind about one game are one card. */
  sid?: string;
  /** The small solid chip ("LIVE", "NEW"). */
  chip?: string;
  /** The pixel headline: two to five words, numbers big. */
  head: string;
  /** At most one line of small detail. */
  detail?: string;
  /** A post's or message's whole text, in mono under the headline (about ten lines, then "more" opens it in place). */
  body?: string;
  /** The one key line, on the tint-filled bar. */
  caption: string;
  /** Muted footer ("alpha · #ideas"). */
  foot?: string;
  onOpen?: () => void;
  /** The first, largest cards of a row. */
  hero?: boolean;
  /** An empty section's one quiet card. */
  quiet?: boolean;
  /** The words on the card were written by another agent: drawn as data. */
  theirs?: boolean;
}

/** A line cut to `n` characters at a word, with an ellipsis. */
export function clip(text: string, n: number): string {
  const t = text.replace(/\s+/g, " ").trim();
  if (t.length <= n) return t;
  const cut = t.slice(0, n).replace(/\s+\S*$/, "");
  return `${cut || t.slice(0, n)}…`;
}

/**
 * Merges a row's cards of one kind (`merge`): the same game twice is one card; several games make one card
 * "YOUR TURN ×2" whose caption names them briefly and which opens the first. Order is kept; a card without
 * `merge` is never touched.
 */
export function mergeCards(cards: CardSpec[]): CardSpec[] {
  const groups = new Map<string, { first: CardSpec; sids: Set<string>; labels: string[]; n: number }>();
  const kept: CardSpec[] = [];
  for (const c of cards) {
    if (!c.merge) { kept.push(c); continue; }
    const g = groups.get(c.merge);
    if (!g) {
      groups.set(c.merge, { first: c, sids: new Set(c.sid ? [c.sid] : []), labels: [c.label ?? c.caption], n: 1 });
      kept.push(c);
      continue;
    }
    if (c.sid && g.sids.has(c.sid)) continue;
    if (c.sid) g.sids.add(c.sid);
    g.labels.push(c.label ?? c.caption);
    g.n += 1;
  }
  return kept.map((c) => {
    const g = c.merge ? groups.get(c.merge) : undefined;
    if (!g || g.first !== c || g.n < 2) return c;
    const names = [...new Set(g.labels)];
    return { ...c, head: `${c.head} ×${g.n}`, caption: clip(names.join(" · "), 64), detail: undefined, label: undefined };
  });
}

/** The quiet card an empty section collapses to. */
export function quietCard(key: string, head: string, caption: string): CardSpec {
  return { key, meta: "quiet", head, caption, quiet: true };
}
