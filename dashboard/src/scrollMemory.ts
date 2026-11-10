/**
 * Going into a drill-down (a game from the Arena, an agent from the list) and back should land on the same
 * spot. The page's scroll is held in memory for the session; reading it never throws and needs no storage.
 */

/** The nearest ancestor that scrolls, else the document's scroller. */
export function scrollerOf(el: Element | null): Element | null {
  for (let n = el?.parentElement ?? null; n; n = n.parentElement) {
    const oy = getComputedStyle(n).overflowY;
    if ((oy === "auto" || oy === "scroll") && n.scrollHeight > n.clientHeight) return n;
  }
  return document.scrollingElement;
}

const SAVED = new Map<string, number>();

export function saveScroll(key: string, from: Element | null): void {
  SAVED.set(key, scrollerOf(from)?.scrollTop ?? 0);
}

/** Put the scroll back once the page it belonged to is drawn again (next frame). */
export function restoreScroll(key: string, from: Element | null): void {
  const top = SAVED.get(key);
  if (top === undefined) return;
  // The page may still be loading its rows: try for about a third of a second.
  let tries = 0;
  const step = () => {
    const s = scrollerOf(from);
    if (s) s.scrollTop = top;
    if (s && Math.abs(s.scrollTop - top) > 1 && ++tries < 20) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
