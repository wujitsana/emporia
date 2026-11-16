// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { Session, SessionAction } from "../api";
import { buildTimeline } from "../arena";
import type { TesseractFrame } from "../tesseract";
import { TesseractPanel } from "./TesseractPanel";
import table from "../fixtures/tesseract/table.json";
import mid from "../fixtures/tesseract/mid.json";
import waiting from "../fixtures/tesseract/waiting.json";

afterEach(cleanup);
type Fx = { session: unknown; actions: unknown };
const frame = (fx: Fx, i?: number) => {
  const t = buildTimeline(fx.session as Session, fx.actions as SessionAction[]);
  return t.frameAt(i ?? t.steps.length - 1) as TesseractFrame;
};
const view = (f: TesseractFrame) => render(<TesseractPanel frame={f} name={(s) => s} />);

describe("TesseractPanel", () => {
  it("opens on the flat view: three columns and the mix as four labeled bars, no target yet", () => {
    const { container } = view(frame(mid));
    expect(screen.getByRole("button", { name: "Flat" }).getAttribute("aria-pressed")).toBe("true");
    expect(container.querySelectorAll("[data-node]").length).toBe(14);
    expect(container.querySelectorAll("[data-edge]").length).toBe(24);
    const bars = screen.getAllByRole("img");
    expect(bars.length).toBe(4);
    expect(bars[0].getAttribute("aria-label")).toMatch(/^W: \d+\.\d percent$/);
    expect(container.querySelector("[data-tick]")).toBeNull();
    expect(container.textContent).toContain("4 inputs");
    expect(container.textContent).toContain("sealed, not shown");
  });

  it("the table view lists the 24 channels with width, net push, owners, dark and pull", () => {
    const { container } = view(frame(mid));
    fireEvent.click(screen.getByRole("button", { name: "Table" }));
    const rows = [...container.querySelectorAll("tbody tr")];
    expect(rows.length).toBe(24);
    const heads = [...container.querySelectorAll("thead th")].map((h) => h.textContent);
    expect(heads).toEqual(["Channel", "Width", "Net push", "Pushed by", "Dark", "Pull for +4", "Pull for −4"]);
    const yz = container.querySelector('[data-channel="YZ>Z"]')!;
    expect(yz.textContent).toContain("fy_0 +3");           // round 2: everyone pushed YZ>Z +3
    expect(within(yz as HTMLElement).getAllByRole("cell")[1].textContent).toBe("+12");
    expect(yz.textContent).toMatch(/W [+−]?\d/);            // the live pull table is shown
  });

  it("a replayed earlier round shows no pull, and the stepper moves between networks", () => {
    const { container } = view(frame(mid));
    fireEvent.click(screen.getByRole("button", { name: "Table" }));
    fireEvent.click(screen.getByRole("button", { name: "round 1" }));
    expect(container.querySelector("caption")!.textContent).toContain("after round 1");
    expect(container.querySelector('[data-channel="WX>W"]')!.textContent).toContain("n/a");
    fireEvent.click(screen.getByRole("button", { name: "opening" }));
    expect(container.querySelector("caption")!.textContent).toContain("as opened");
  });

  it("shows targets as ticks, and a chooser, only when the table is over", () => {
    const { container } = view(frame(table));
    expect(container.querySelectorAll("[data-tick]").length).toBe(4);
    expect(screen.getByLabelText(/Target of/)).toBeTruthy();
    expect(container.textContent).toMatch(/distance \d+ basis points/);
    cleanup();
    const early = view(frame(table, 10));
    expect(early.container.querySelector("[data-tick]")).toBeNull();
    expect(screen.queryByLabelText(/Target of/)).toBeNull();
  });

  it("labels a practice table, and says the network comes later on a waiting one", () => {
    const fx = { session: { ...(mid.session as any), config: { ...(mid.session as any).config, practice: true } }, actions: mid.actions };
    const { container } = view(frame(fx));
    expect(container.textContent).toContain("Practice table: counts for nothing");
    cleanup();
    const w = view(frame(waiting));
    expect(w.container.textContent).toContain("network is dealt once every seat has sealed");
    expect(w.container.querySelector("[data-node]")).toBeNull();
  });

  it("3D is a keyboard-focusable group, and picking it draws the tetrahedron, the mix and the corners", () => {
    const { container } = view(frame(table));
    fireEvent.click(screen.getByRole("button", { name: "3D" }));
    expect(screen.getByTestId("graph3d-stage").getAttribute("tabindex")).toBe("0");
    expect(container.querySelectorAll("[data-decor] polygon").length).toBe(4);
    expect(container.querySelectorAll("[data-decor-points] rect").length).toBe(2);
    expect(container.querySelectorAll("[data-decor-points] circle").length).toBe(2);   // the mix and one target ring
    expect(container.textContent).toContain("which corner the mix is heading toward");
  });
});
