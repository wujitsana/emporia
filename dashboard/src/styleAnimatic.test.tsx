// @vitest-environment jsdom
/**
 * The animatic style is additive: with the default style on, none of its rules
 * can match and the shared components render exactly what they did before.
 */
import { readFileSync } from "fs";
import path from "path";
import postcss, { type Rule } from "postcss";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { navById } from "./navConfig";
import { scopeToShadowHost } from "./shadowScope";
import { _resetStyleForTests, applyStyle, setStyle } from "./styleMode";
import { AnimaticHudFoot } from "./ui/AnimaticChrome";
import { PageHeading } from "./ui/layout";
import { OverviewIntro } from "./views/OverviewView";

const CSS = readFileSync(path.resolve(__dirname, "styleAnimatic.css"), "utf8");
const SCOPE = "style-animatic";

/** Every selector in the file, with the at-rule it sits in. */
function selectors(): { sel: string; inside: string }[] {
  const out: { sel: string; inside: string }[] = [];
  postcss.parse(CSS).walkRules((rule: Rule) => {
    const parent = rule.parent as { type?: string; name?: string } | undefined;
    if (parent?.type === "atrule" && /keyframes$/i.test(parent.name ?? "")) return;
    for (const sel of rule.selectors) out.push({ sel, inside: parent?.type === "atrule" ? `@${parent.name}` : "" });
  });
  return out;
}

/** The Settings switch is the one new control drawn in both styles. */
const BOTH_STYLES = /^\.e-style-switch(__option)?\b/;

// jsdom here has no localStorage (see integrityCheck.test.ts): a small stand-in.
beforeEach(() => {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
  _resetStyleForTests();
  document.body.className = "";
});

describe("styleAnimatic.css is scoped", () => {
  it("has rules, and every one is under .style-animatic (but the Settings switch's own)", () => {
    const all = selectors();
    expect(all.length).toBeGreaterThan(80);
    const unscoped = all.filter(({ sel }) => !sel.includes(`.${SCOPE}`) && !BOTH_STYLES.test(sel));
    expect(unscoped).toEqual([]);
  });

  it("stays scoped after the Hermes tab's shadow-root rewrite", () => {
    for (const { sel } of selectors()) {
      if (BOTH_STYLES.test(sel)) continue;
      expect(scopeToShadowHost(sel)).toContain(`.${SCOPE}`);
    }
  });

  it("loads no remote font: the pixel face is bundled", () => {
    expect(CSS).not.toMatch(/url\(\s*['"]?(https?:)?\/\//i);
    expect(CSS).toMatch(/url\("\.\/assets\/fonts\/Silkscreen-Regular\.woff2"\)/);
  });
});

/** A page made of the shared parts the style touches. */
function page() {
  return render(
    <div className="e-app-shell e-dashboard-shell">
      <PageHeading item={navById("overview")} />
      <OverviewIntro go={() => {}} />
      <section className="e-now"><header className="e-now__head"><span>Your agent now</span><span>alpha</span></header>
        <p className="e-now__row"><span className="e-now__label">next</span></p></section>
      <div className="e-setplay"><p className="e-setplay__game">Blotto</p>
        <p className="e-setplay__verdict is-win">alpha wins</p></div>
      <p className="e-subhead">QUESTS</p><p className="e-quest-row is-done">✓ Say hello</p>
      <AnimaticHudFoot section="overview" />
    </div>,
  );
}

/** The selectors as a query can run them: pseudo-elements dropped. */
function queryable(sel: string): string {
  return sel.replace(/::?(before|after|-webkit-[\w-]+)\b/g, "").trim() || "*";
}

describe("the default style renders as before", () => {
  it("draws the section heading exactly as it was", () => {
    const { container } = render(<PageHeading item={navById("overview")} />);
    expect(container.innerHTML).toBe(
      '<header class="e-page-head"><h1 class="e-page-head__title">'
      + '<span class="e-page-head__icon" aria-hidden="true">✶</span>Overview</h1>'
      + '<p class="e-page-head__blurb">Your agent now, and what is happening on this relay — start here</p></header>',
    );
  });

  it("draws no status lines and no cursor", () => {
    const { container } = page();
    expect(container.querySelector(".e-anim-hud, .e-anim-cursor, .e-anim-flash")).toBeNull();
  });

  it("matches none of the animatic rules with the style off, and they match once it is on", () => {
    applyStyle("retromod", document.body);
    page();
    const scoped = selectors().filter(({ sel }) => !BOTH_STYLES.test(sel));
    const hits = scoped.filter(({ sel }) => document.querySelector(queryable(sel)) !== null);
    expect(hits).toEqual([]);

    act(() => setStyle("animatic", { save: false }));
    applyStyle("animatic", document.body);
    const on = scoped.filter(({ sel }) => document.querySelector(queryable(sel)) !== null);
    expect(on.length).toBeGreaterThan(20);
    expect(document.querySelector(".e-anim-hud--top")).not.toBeNull();
    expect(document.querySelector(".e-anim-hud--foot")?.textContent).toMatch(/^T\+00:0\d/);
  });
});
