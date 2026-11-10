/**
 * Which navigation the dashboard draws, from the width of the dashboard's OWN frame.
 *
 * Inside the Hermes tab the browser window is wider than the dashboard (Hermes's own sidebar takes
 * the left), so a viewport media query picks a layout for room the dashboard does not have. The
 * frame (`.e-app-main`) is measured instead, with a ResizeObserver, and the answer is written on the
 * shell as `data-nav` for dashboard.css.
 *
 *   wide    five labels in the header row, the sub-tabs in the row below
 *   medium  the same five labels, tighter, in the header row (no icons beside words)
 *   rail    a vertical icon rail on the left; labels on hover and focus
 *   phone   the bottom bar, and the menu behind the burger
 *
 * Every layout draws all five destinations and none of them scrolls: there is no gap between the
 * ranges (each width belongs to exactly one) and no range hides a destination.
 */
import { useEffect, useLayoutEffect, useState, type RefObject } from "react";
import { isNarrow } from "./overlayFocus";

export type NavLayout = "wide" | "medium" | "rail" | "phone";

/** The smallest frame width, in px, at which each layout is drawn. */
export const NAV_MIN_WIDTH = { wide: 1240, medium: 1000, rail: 680, phone: 0 } as const;

/** The layout for a frame `width` px wide. Not a measurement yet (0, NaN): `fallbackPhone` decides. */
export function navLayoutFor(width: number, fallbackPhone = false): NavLayout {
  if (!Number.isFinite(width) || width <= 0) return fallbackPhone ? "phone" : "wide";
  if (width >= NAV_MIN_WIDTH.wide) return "wide";
  if (width >= NAV_MIN_WIDTH.medium) return "medium";
  if (width >= NAV_MIN_WIDTH.rail) return "rail";
  return "phone";
}

/** The word a destination has when the header row is short of room (the phone's bar says the same). */
export function shortLabel(d: { id: string; label: string }): string {
  return d.id === "labs" ? "Season" : d.label;
}

export type SubEdges = { left: boolean; right: boolean; hidden: string[] };

export function sameEdges(a: SubEdges, b: SubEdges): boolean {
  return a.left === b.left && a.right === b.right && a.hidden.join("|") === b.hidden.join("|");
}

/**
 * What the sub-tab strip cuts off at its left and right edge, read from its own geometry. A tab counts
 * as cut off when under 90 % of it shows; the strip lists those in its "more" menu.
 */
export function readSubEdges(nav: HTMLElement): SubEdges {
  const view = nav.clientWidth;
  const hidden: string[] = [];
  nav.querySelectorAll<HTMLElement>(".e-subnav__tab").forEach((tab) => {
    const left = tab.offsetLeft - nav.scrollLeft;
    const right = left + tab.offsetWidth;
    const shown = Math.min(right, view) - Math.max(left, 0);
    if (tab.offsetWidth > 0 && shown < tab.offsetWidth * 0.9) hidden.push(tab.dataset.key ?? "");
  });
  return {
    left: nav.scrollLeft > 1,
    right: nav.scrollLeft + view < nav.scrollWidth - 1,
    hidden,
  };
}

/**
 * The layout of `frame`, following its width. Before the first measurement (and where nothing can
 * be measured, as in jsdom) it falls back to the window's own phone width.
 */
export function useNavLayout(frame: RefObject<HTMLElement | null>): NavLayout {
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = frame.current;
    if (!el) return;
    const read = () => setWidth(el.getBoundingClientRect().width);
    read();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", read);
      return () => window.removeEventListener("resize", read);
    }
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [frame]);
  // Re-read once the page has settled: a Hermes tab sizes its frame after the first paint.
  useEffect(() => {
    const el = frame.current;
    if (!el) return;
    const t = setTimeout(() => setWidth(el.getBoundingClientRect().width), 120);
    return () => clearTimeout(t);
  }, [frame]);
  return navLayoutFor(width, isNarrow());
}
