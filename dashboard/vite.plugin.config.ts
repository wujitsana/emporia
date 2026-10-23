/**
 * P126: the Emporia dashboard bundled as a Hermes dashboard plugin.
 *
 *   npm run build:hermes-plugin   →  dist-plugin/index.js, style.css, assets/
 *
 * One classic-script IIFE (Hermes loads plugin entries with <script async>)
 * with its own React, mounted in a shadow root by src/hermesPlugin.tsx. The CSS
 * is rewritten for that root: `html` and `:root` become `:host`, `body` becomes
 * the app's container (.e-host-body), so SRCL's and the themes' global rules
 * apply inside the shadow root and nowhere else.
 */
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { noRemoteFonts } from "./postcss.noRemoteFonts";
import { scopeToShadowHost } from "./src/shadowScope";

const SRCL = path.resolve("node_modules/srcl");

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
  plugins: [react()],
  resolve: {
    alias: {
      "@components": path.join(SRCL, "components"),
      "@common": path.join(SRCL, "common"),
      "@modules": path.join(SRCL, "modules"),
    },
  },
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  css: {
    modules: { localsConvention: "camelCase" },
    postcss: { plugins: [noRemoteFonts, shadowScope] },
  },
  build: {
    outDir: "dist-plugin",
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
