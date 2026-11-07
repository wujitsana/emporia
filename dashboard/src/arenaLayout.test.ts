import { describe, expect, it } from "vitest";

type Fs = { readFileSync(p: string, enc: "utf8"): string };
const proc = (globalThis as unknown as { process: { cwd(): string; getBuiltinModule(m: string): unknown } }).process;
const css = (proc.getBuiltinModule("node:fs") as Fs).readFileSync(`${proc.cwd()}/src/dashboard.css`, "utf8");

describe("the Arena fits a phone", () => {
  it("lets every box shrink and every long name wrap, so nothing scrolls the page sideways", () => {
    expect(css).toMatch(/\.e-arena, \.e-arena \* \{ min-width: 0; overflow-wrap: anywhere; \}/);
  });
  it("stacks the stage and the moves in one column until there is room for two", () => {
    expect(css).toMatch(/\.e-arena-cols \{[^}]*grid-template-columns: minmax\(0, 1fr\);/);
    expect(css).toMatch(/@media \(min-width: 900px\) \{ \.e-arena-cols/);
  });
});
