// @vitest-environment jsdom
/**
 * The navigation follows the dashboard's own width. Every width from 320 to 1600 px gets exactly one
 * layout, and each layout draws all five places without scrolling them.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SectionTabs, BottomBar } from "../AppNav";
import { NAV_MIN_WIDTH, navLayoutFor, type NavLayout } from "./navLayout";
import { readSubEdges } from "./navLayout";

afterEach(cleanup);

const WIDTHS = Array.from({ length: (1600 - 320) / 20 + 1 }, (_, i) => 320 + i * 20);
const FIVE = ["Home", "Messages", "Agora", "Arena", "Agent"];

/** What each layout draws its five places with (what AppNav renders, and which of them CSS shows). */
function drawn(layout: NavLayout): string[] {
  const props = { view: "overview" as const, onGoDest: () => {} };
  if (layout === "phone") {
    const { container } = render(<BottomBar {...props} />);
    return [...container.querySelectorAll(".e-bottom-bar__tab")].map((b) => b.getAttribute("aria-label") ?? "");
  }
  const { container } = render(<SectionTabs {...props} variant={layout === "rail" ? "rail" : "horizontal"} />);
  return [...container.querySelectorAll(".e-section-tab")].map((b) => b.getAttribute("aria-label") ?? b.querySelector(".e-section-tab__label")?.textContent ?? "");
}

describe("the width to layout mapping", () => {
  it("gives every width 320 to 1600 px (20 px steps) one of the four layouts", () => {
    for (const w of WIDTHS) expect(["wide", "medium", "rail", "phone"]).toContain(navLayoutFor(w));
  });

  it("only widens as the frame widens, with no gap between the ranges", () => {
    const order: NavLayout[] = ["phone", "rail", "medium", "wide"];
    let last = 0;
    for (let w = 1; w <= 2000; w++) {
      const at = order.indexOf(navLayoutFor(w));
      expect(at).toBeGreaterThanOrEqual(last);
      last = at;
    }
    expect(navLayoutFor(NAV_MIN_WIDTH.rail - 1)).toBe("phone");
    expect(navLayoutFor(NAV_MIN_WIDTH.rail)).toBe("rail");
    expect(navLayoutFor(NAV_MIN_WIDTH.medium - 1)).toBe("rail");
    expect(navLayoutFor(NAV_MIN_WIDTH.medium)).toBe("medium");
    expect(navLayoutFor(NAV_MIN_WIDTH.wide - 1)).toBe("medium");
    expect(navLayoutFor(NAV_MIN_WIDTH.wide)).toBe("wide");
  });

  it("makes all five destinations reachable at every width, without scrolling the main navigation", () => {
    for (const w of WIDTHS) {
      const layout = navLayoutFor(w);
      const names = drawn(layout);
      expect(names, `${w}px (${layout})`).toHaveLength(5);
      // Every layout names the same five places, in the same order.
      expect(names, `${w}px`).toEqual(FIVE);
      cleanup();
    }
  });

  it("falls back to the window's own phone test before the frame has a measured width", () => {
    expect(navLayoutFor(0)).toBe("wide");
    expect(navLayoutFor(0, true)).toBe("phone");
    expect(navLayoutFor(Number.NaN, true)).toBe("phone");
  });

  it("names the sub-tabs the strip cuts off, so the 'more' menu lists them", () => {
    const nav = document.createElement("nav");
    const num = (el: HTMLElement, props: Record<string, number>) =>
      Object.entries(props).forEach(([k, v]) => Object.defineProperty(el, k, { value: v, configurable: true }));
    // Tabs 100 px wide each (Feed, Topics, Rooms, Agents); the strip shows 250 px.
    ["feed", "agora", "rooms", "agents"].forEach((key, i) => {
      const b = document.createElement("button");
      b.className = "e-subnav__tab";
      b.dataset.key = key;
      num(b, { offsetLeft: i * 100, offsetWidth: 100 });
      nav.appendChild(b);
    });
    num(nav, { clientWidth: 250, scrollWidth: 400, scrollLeft: 0 });
    expect(readSubEdges(nav)).toEqual({ left: false, right: true, hidden: ["rooms", "agents"] });
    num(nav, { scrollLeft: 150 });
    expect(readSubEdges(nav)).toEqual({ left: true, right: false, hidden: ["feed", "agora"] });
    num(nav, { clientWidth: 400, scrollLeft: 0 });
    expect(readSubEdges(nav)).toEqual({ left: false, right: false, hidden: [] });
  });

  it("labels the rail's icons for keyboard and screen-reader users", () => {
    const { container } = render(<SectionTabs view="games" onGoDest={() => {}} variant="rail" />);
    const nav = container.querySelector("nav")!;
    expect(nav.getAttribute("aria-label")).toBe("Places");
    expect(nav.querySelector("[aria-current=page]")?.getAttribute("aria-label")).toBe("Arena");   // view "games" lives in Arena
    expect(nav.querySelectorAll(".e-section-tab__tip")).toHaveLength(5);
  });
});
