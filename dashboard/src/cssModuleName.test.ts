import { describe, expect, it } from "vitest";
import { cssModuleName } from "./cssModuleName";

// Vitest blanks CSS imports (even ?raw), and this tsconfig has no node types —
// so the two stylesheets are read through Node's own module getter, typed narrowly.
type Fs = { readFileSync(p: string, enc: "utf8"): string; existsSync(p: string): boolean };
const proc = (globalThis as unknown as { process: { cwd(): string; getBuiltinModule(m: string): unknown } }).process;
const fs = proc.getBuiltinModule("node:fs") as Fs;
const SRC = `${proc.cwd()}/src`;
const dashboardCss = fs.readFileSync(`${SRC}/dashboard.css`, "utf8");
const kitModule = (comp: string): string | null => {
  const p = `${SRC}/ui/kit/${comp}.module.css`;
  return fs.existsSync(p) ? fs.readFileSync(p, "utf8") : null;
};

describe("cssModuleName", () => {
  it("names a kit class after its component", () => {
    const n = cssModuleName("sidebar", "/x/src/ui/kit/SidebarLayout.module.css", ".sidebar{}");
    expect(n).toMatch(/^SidebarLayout_sidebar_[a-z0-9]{1,5}$/);
  });

  it("is stable and ignores a query string", () => {
    const a = cssModuleName("root", "/k/ActionButton.module.css?used", "x");
    const b = cssModuleName("root", "/k/ActionButton.module.css", "x");
    expect(a).toBe(b);
  });

  // The defect this exists for: dashboard.css restyles the kit with
  // [class*="Component_local"] selectors, and after the kit was vendored none of
  // them matched — the mobile stacking of split views included. Every such
  // selector must name a real kit module and a class it defines.
  it("every [class*=…] selector in dashboard.css names a real kit class", () => {
    const refs = [...new Set([...dashboardCss.matchAll(/class\*="([A-Za-z]+)_([A-Za-z]+)"/g)].map((m) => `${m[1]}_${m[2]}`))];
    expect(refs.length).toBeGreaterThan(0);
    const missing = refs.filter((ref) => {
      const [comp, local] = ref.split("_");
      const src = kitModule(comp);
      return !src || !new RegExp(`\\.${local}\\b`).test(src);
    });
    expect(missing).toEqual([]);
  });
});
