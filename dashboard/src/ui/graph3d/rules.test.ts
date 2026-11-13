import { describe, expect, it } from "vitest";
import { flatLayout } from "./flatLayout";
import { SMALL_GRAPH } from "./fixture";

// Vitest blanks CSS imports, so read the files through Node's own module getter, typed narrowly.
type Fs = { readFileSync(p: string, enc: "utf8"): string; readdirSync(p: string): string[] };
const proc = (globalThis as unknown as { process: { cwd(): string; getBuiltinModule(m: string): unknown } }).process;
const fs = proc.getBuiltinModule("node:fs") as Fs;
const DIR = `${proc.cwd()}/src/ui/graph3d`;
const files = fs.readdirSync(DIR).filter((f) => /\.(tsx?|css)$/.test(f) && !/\.test\./.test(f));

describe("graph3d rules", () => {
  it("imports no react-dom (the Desktop page forbids it)", () => {
    for (const f of files) expect(fs.readFileSync(`${DIR}/${f}`, "utf8"), f).not.toMatch(/react-dom/);
  });
  it("styles use theme tokens only: no hex, rgb() or named colours", () => {
    for (const f of files.filter((x) => x.endsWith(".css"))) {
      const css = fs.readFileSync(`${DIR}/${f}`, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
      expect(css, f).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/);
      expect(css, f).not.toMatch(/:\s*(red|blue|green|black|white|gray|grey|orange|yellow)\b/);
    }
  });
  it("flat layout puts layers left to right inside the box", () => {
    const { placed } = flatLayout(SMALL_GRAPH.nodes);
    const x = (layer: number) => placed[SMALL_GRAPH.nodes.findIndex((n) => n.layer === layer)].x;
    expect(x(0)).toBeLessThan(x(1));
    expect(x(2)).toBeLessThan(x(3));
    for (const q of placed) expect(q.y).toBeGreaterThan(0);
  });
});
