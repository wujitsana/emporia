/**
 * P126: the Emporia dashboard bundled as a Hermes dashboard plugin.
 *
 *   npm run build:hermes-plugin   →  ../hermes_plugin/emporia/dashboard/dist/{index.js,style.css}
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
import fs from "fs";
import crypto from "crypto";
import { noRemoteFonts } from "./postcss.noRemoteFonts";
import { scopeToShadowHost } from "./src/shadowScope";

const KIT = path.resolve("src/ui/kit");

/** P295: what the committed bundle was built from — tests/test_dashboard_plugin.py
 *  recomputes it and fails when dashboard/ changed without a rebuild. */
export const SOURCE_INPUTS = ["src", "vite.plugin.config.ts", "package-lock.json", "postcss.noRemoteFonts.ts"];

function sourceDigest(): string {
  const files: string[] = [];
  const walk = (rel: string) => {
    const st = fs.statSync(rel);
    if (st.isDirectory()) fs.readdirSync(rel).sort().forEach((n) => walk(path.posix.join(rel, n)));
    else if (!/\.test\.tsx?$/.test(rel)) files.push(rel);
  };
  SOURCE_INPUTS.forEach(walk);
  const h = crypto.createHash("sha256");
  for (const f of files.sort()) {
    h.update(f + "\n");
    h.update(fs.readFileSync(f));
  }
  return h.digest("hex");
}

const writeSourceDigest = {
  name: "emporia-source-digest",
  closeBundle() {
    fs.writeFileSync("../hermes_plugin/emporia/dashboard/dist/SOURCE", sourceDigest() + "\n");
  },
};

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
  plugins: [react(), writeSourceDigest],
  resolve: {
    alias: [
      { find: "@kit", replacement: KIT },
      // P295: the tab's bundle carries no doc text; its console serves the docs.
      { find: /^\.\/docTexts$/, replacement: path.resolve("src/docTexts.plugin.ts") },
    ],
  },
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  css: {
    modules: { localsConvention: "camelCase" },
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
        inlineDynamicImports: true,
        assetFileNames: (a) => (a.names?.[0] ?? a.name ?? "").endsWith(".css")
          ? "style.css" : "assets/[name][extname]",
      },
    },
  },
});
