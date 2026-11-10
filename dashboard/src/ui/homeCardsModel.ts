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
  /** Status line, right: a time or a clock. */
  time?: string;
  /** The small solid chip ("LIVE", "NEW"). */
  chip?: string;
  /** The pixel headline: two to five words, numbers big. */
  head: string;
  /** At most one line of small detail. */
  detail?: string;
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

/** The quiet card an empty section collapses to. */
export function quietCard(key: string, head: string, caption: string): CardSpec {
  return { key, meta: "quiet", head, caption, quiet: true };
}
