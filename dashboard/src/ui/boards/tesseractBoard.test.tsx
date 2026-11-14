// @vitest-environment jsdom
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { Session, SessionAction } from "../../api";
import { buildTimeline } from "../../arena";
import type { TesseractFrame } from "../../tesseract";
import { ArenaFrame } from "../ArenaFrames";
import { TesseractBoard } from "./TesseractBoard";
import { tesseractFromFrame } from "./tesseractModel";
import table from "../../fixtures/tesseract/table.json";
import mid from "../../fixtures/tesseract/mid.json";
import waiting from "../../fixtures/tesseract/waiting.json";

// No node types in this tsconfig: Node's own fs, typed narrowly (as look.test.ts does).
type Fs = { readFileSync(p: string, enc: "utf8"): string };
const proc = (globalThis as unknown as { process: { cwd(): string; getBuiltinModule(m: string): unknown } }).process;
const fs = proc.getBuiltinModule("node:fs") as Fs;

afterEach(cleanup);

const tl = (fx: { session: unknown; actions: unknown }) => buildTimeline(fx.session as Session, fx.actions as SessionAction[]);
const props = (fx: { session: unknown; actions: unknown }, i?: number) => {
  const t = tl(fx);
  return tesseractFromFrame(t.frameAt(i ?? t.steps.length - 1) as TesseractFrame, (s) => s);
};
const rows = (c: HTMLElement) => [...c.querySelectorAll("table tbody tr")].map((r) => [...r.children].map((x) => x.textContent));

describe("TesseractBoard", () => {
  it("states every mix in a table, in words and numbers", () => {
    const { container } = render(<TesseractBoard {...props(table)} />);
    const r = rows(container);
    expect(r[0][0]).toBe("opening");
    expect(r.length).toBe(6);
    expect(r[1][0]).toBe("round 1");
    expect(r[1].slice(1, 5).every((x) => /^\d+\.\d %$/.test(x ?? ""))).toBe(true);
    expect(container.querySelector("table caption")?.textContent).toContain("5 rounds played");
  });

  it("the finished table says who won, each total and index, and the revealed targets", () => {
    const { container } = render(<TesseractBoard {...props(table)} />);
    expect(container.querySelector("[role=status]")?.textContent).toMatch(/won with -?\d+ points|tied at/);
    expect(container.querySelectorAll("ol li").length).toBe(4);
    expect(container.textContent).toMatch(/table index \d+ · target W \d+ X \d+ Y \d+ Z \d+/);
    expect(container.textContent).toContain("sat out:");
    expect(container.textContent).toContain("put out:");
  });

  it("a sealed seat is hatched and says sealed, a waiting table has no mix to draw", () => {
    const m = render(<TesseractBoard {...props(mid)} />);
    expect(m.container.textContent).toContain("Round 3 of 5: sealing the push.");
    expect([...m.container.querySelectorAll("li")].filter((li) => li.textContent?.includes("sealed")).length).toBe(2);
    cleanup();
    const w = render(<TesseractBoard {...props(waiting, 0)} />);
    expect(w.container.textContent).not.toContain("round 1");
  });

  it("uses theme variables only: no colour value and no amber in its own style", () => {
    const css = fs.readFileSync(`${proc.cwd()}/src/ui/boards/TesseractBoard.module.css`, "utf8");
    expect(css).not.toMatch(/#[0-9a-f]{3,8}\b|rgb\(|hsl\(|amber|orange|oklch\(/i);
    expect(css).toMatch(/var\(--/);
  });

  it("is drawn by the Arena for a tesseract frame and is read-only", () => {
    const t = tl(table);
    const { container } = render(<ArenaFrame frame={t.frameAt(t.steps.length - 1)} book={undefined} />);
    expect(container.querySelector("[data-kind=tesseract]")).not.toBeNull();
    expect(container.querySelectorAll("button, input, a, form").length).toBe(0);
  });
});
