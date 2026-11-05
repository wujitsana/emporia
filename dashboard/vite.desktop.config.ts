/**
 * The Emporia dashboard as a Hermes Desktop page (and the browser Webapp, which
 * runs the same renderer):
 *
 *   npm run build:plugin   →  ../hermes_plugin/emporia/desktop/plugin.js  (+ SOURCE)
 *
 * The desktop half of the same plugin package: Desktop copies a package's
 * `desktop/` folder into its desktop-plugins root and refreshes it when the
 * plugin updates (it stays off until the user enables it in Capabilities ▸
 * Plugins). Committed, like the tab's bundle, and built from the same
 * dashboard/src; only src/desktop/ and the `desktop` mode in transport.ts and
 * dashboardRoute.ts are its own.
 *
 * What the Desktop loader and the catalog's `desktop surface` lint (rule 8)
 * require, and how this build meets it:
 * - one plain ESM file whose only imports are `@hermes/plugin-sdk`, `react` and
 *   `react/jsx-runtime`: those three are external, everything else is bundled
 *   in, and dynamic imports are inlined;
 * - the host's React renders it, through the SDK's page area: no second React,
 *   and no react-dom at all (its internals read as script injection to the lint);
 *   importing it fails this build;
 * - no shadow root: the CSS is inlined as a string the page renders in a
 *   <style>, every rule scoped under `.e-desktop-root` (src/shadowScope.ts
 *   scopeToContainer), so it reaches nothing of Desktop's;
 * - not minified, so a reviewer reads what runs.
 */
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { SHARED_INPUTS, pluginVersion, writeSourceDigest } from "./buildMeta.ts";
import { noRemoteFonts } from "./postcss.noRemoteFonts.ts";
import { scopeToContainer } from "./src/shadowScope.ts";
import { cssModuleName } from "./src/cssModuleName.ts";

const KIT = path.resolve("src/ui/kit");
const OUT = "../hermes_plugin/emporia/desktop";
const HOST_MODULES = ["@hermes/plugin-sdk", "react", "react/jsx-runtime"];
/** What src/desktop/css.ts exports until this build swaps in the stylesheet. */
const CSS_PLACEHOLDER = "__EMPORIA_DESKTOP_CSS__";

/** What the committed page was built from — tests/test_dashboard_plugin.py recomputes it. */
export const SOURCE_INPUTS = [...SHARED_INPUTS, "vite.desktop.config.ts"];

const containerScope = {
  postcssPlugin: "emporia-container-scope",
  Rule(rule: {
    selectors: string[];
    parent?: { type?: string; name?: string };
    source?: { input?: { file?: string } };
  }) {
    // Keyframe steps are not selectors, and a nested rule (`&:focus`) is scoped by its parent.
    if (rule.parent?.type === "atrule" && /keyframes$/i.test(rule.parent.name ?? "")) return;
    if (rule.parent?.type === "rule") return;
    const cssModule = /\.module\.css$/.test(rule.source?.input?.file ?? "");
    rule.selectors = rule.selectors.map((s) => scopeToContainer(s, { cssModule }));
  },
};

/** react-dom must never reach the page: the host renders it with its own React. */
const noReactDom: Plugin = {
  name: "emporia-no-react-dom",
  enforce: "pre",
  resolveId(id, importer) {
    if (id === "react-dom" || id.startsWith("react-dom/")) {
      throw new Error(`react-dom imported by ${importer}: the Desktop page renders through the host's React`);
    }
    return null;
  },
};

/** The stylesheet into the one JS file, as the string src/desktop/css.ts exports. */
const inlineCss: Plugin = {
  name: "emporia-inline-css",
  enforce: "post",
  generateBundle(_opts, bundle) {
    const sheets = Object.keys(bundle).filter((k) => k.endsWith(".css"));
    const css = sheets.map((k) => {
      const a = bundle[k];
      return a.type === "asset" ? String(a.source) : "";
    }).join("\n");
    sheets.forEach((k) => delete bundle[k]);
    const entry = Object.values(bundle).find((c) => c.type === "chunk" && c.isEntry);
    if (!entry || entry.type !== "chunk") throw new Error("no entry chunk");
    const quoted = new RegExp(`(["'])${CSS_PLACEHOLDER}\\1`);
    if (!css || !quoted.test(entry.code)) throw new Error("the stylesheet was not inlined into plugin.js");
    entry.code = entry.code.replace(quoted, () => JSON.stringify(css));
  },
};

export default defineConfig({
  base: "./",
  plugins: [noReactDom, react(), inlineCss, writeSourceDigest(SOURCE_INPUTS, `${OUT}/SOURCE`)],
  resolve: {
    alias: [
      { find: "@kit", replacement: KIT },
      // The page's bundle carries no doc text either; the console serves it as JSON.
      { find: /^\.\/docTexts$/, replacement: path.resolve("src/docTexts.plugin.ts") },
    ],
  },
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
    "import.meta.env.VITE_EMPORIA_MODE": JSON.stringify("desktop"),
    "import.meta.env.VITE_RELAY_URL": JSON.stringify(""),
    "import.meta.env.VITE_AGENT_ID": "undefined",
    __EMPORIA_VERSION__: JSON.stringify(pluginVersion()),
  },
  css: {
    modules: { localsConvention: "camelCase", generateScopedName: cssModuleName },
    postcss: { plugins: [noRemoteFonts, containerScope] },
  },
  publicDir: false,
  build: {
    outDir: OUT,
    minify: false,
    emptyOutDir: true,
    cssCodeSplit: false,
    lib: {
      entry: "src/desktop/plugin.tsx",
      formats: ["es"],
      fileName: () => "plugin.js",
    },
    rollupOptions: {
      external: HOST_MODULES,
      output: { codeSplitting: false },
    },
  },
});
