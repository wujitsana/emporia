/**
 * P126: the Emporia dashboard bundled as a Hermes dashboard plugin.
 *
 *   npm run build:plugin   →  ../hermes_plugin/emporia/dashboard/dist/{index.js,style.css}
 *                             (and the Desktop page, vite.desktop.config.ts, and the console)
 *
 * P295: built into the plugin folder and committed — a catalog install is that
 * folder alone, at a pinned commit. Not minified, so a reviewer reads what runs;
 * no public/ files (favicons are the console's, not the tab's).
 *
 * One classic-script IIFE (Hermes loads plugin entries with <script async>)
 * with its own React, mounted in a shadow root by src/hermesPlugin.tsx. The CSS
 * is rewritten for that root: `html` and `:root` become `:host`, `body` becomes
 * the app's container (.e-host-body), so the kit's and the themes' global rules
 * apply inside the shadow root and nowhere else.
 */
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { SHARED_INPUTS, pluginVersion, writeSourceDigest } from "./buildMeta.ts";
import { noRemoteFonts } from "./postcss.noRemoteFonts.ts";
import { scopeToShadowHost } from "./src/shadowScope.ts";
import { cssModuleName } from "./src/cssModuleName.ts";

const KIT = path.resolve("src/ui/kit");

/** P295: what the committed bundle was built from — tests/test_dashboard_plugin.py
 *  recomputes it and fails when dashboard/ changed without a rebuild. */
export const SOURCE_INPUTS = [...SHARED_INPUTS, "vite.plugin.config.ts"];

const shadowScope = {
  postcssPlugin: "emporia-shadow-scope",
  Rule(rule: { selectors: string[]; parent?: { type?: string; name?: string } }) {
    // Keyframe steps are not selectors.
    if (rule.parent?.type === "atrule" && /keyframes$/i.test(rule.parent.name ?? "")) return;
    rule.selectors = rule.selectors.map(scopeToShadowHost);
  },
};

export default defineConfig({
  base: "./",
  plugins: [react(), writeSourceDigest(SOURCE_INPUTS, "../hermes_plugin/emporia/dashboard/dist/SOURCE")],
  resolve: {
    alias: [
      { find: "@kit", replacement: KIT },
      // P295: the tab's bundle carries no doc text; its console serves the docs.
      { find: /^\.\/docTexts$/, replacement: path.resolve("src/docTexts.plugin.ts") },
    ],
  },
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
    __EMPORIA_VERSION__: JSON.stringify(pluginVersion()),
  },
  css: {
    modules: { localsConvention: "camelCase", generateScopedName: cssModuleName },
    postcss: { plugins: [noRemoteFonts, shadowScope] },
  },
  publicDir: false,
  build: {
    outDir: "../hermes_plugin/emporia/dashboard/dist",
    minify: false,
    emptyOutDir: true,
    cssCodeSplit: false,
    lib: {
      entry: "src/hermesPlugin.tsx",
      name: "EmporiaHermesPlugin",
      formats: ["iife"],
      fileName: () => "index.js",
    },
    rollupOptions: {
      output: {
        codeSplitting: false,
        assetFileNames: (a) => (a.names?.[0] ?? a.name ?? "").endsWith(".css")
          ? "style.css" : "assets/[name][extname]",
      },
    },
  },
});
