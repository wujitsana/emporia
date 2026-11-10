/**
 * The browser tab's title carries what needs the agent: "(2) Emporia" for a move or reveal waiting, a deal
 * waiting on a payment or a confirm, and unread messages; plain "Emporia" when nothing does. Only on the
 * console's and the relay's own page: inside Hermes (the tab, the desktop page) the page title is Hermes's.
 */
import { useEffect } from "react";
import { MODE } from "./transport";

/** What waits on the agent: its turns (move, reveal), its deals (payment, confirm) and its unread messages. */
export function attentionCount(items: { kind: string }[], unread: number): number {
  const waiting = items.filter((i) => i.kind === "move" || i.kind === "reveal" || i.kind === "deal").length;
  return waiting + Math.max(0, unread);
}

export function titleFor(n: number): string {
  return n > 0 ? `(${n > 99 ? "99+" : n}) Emporia` : "Emporia";
}

export function useAttentionTitle(n: number): void {
  useEffect(() => {
    if (MODE !== "console" && MODE !== "relay") return;
    document.title = titleFor(n);
  }, [n]);
}
