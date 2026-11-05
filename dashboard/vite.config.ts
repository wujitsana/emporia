import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import { noRemoteFonts } from "./postcss.noRemoteFonts.ts";
import { cssModuleName } from "./src/cssModuleName.ts";
import { pluginVersion } from "./buildMeta.ts";

const RELAY = process.env.VITE_RELAY_URL ?? "http://127.0.0.1:8088";
const KIT = path.resolve("src/ui/kit");

// P261: no relay serves the dashboard, so there is no /ui/ base any more.
export default defineConfig({
  base: "/",
  plugins: [react()],
  // The console says the same version as the tab and the Desktop page (buildMeta.ts).
  define: { __EMPORIA_VERSION__: JSON.stringify(pluginVersion()) },
  resolve: {
    alias: {
      "@kit": KIT,
    },
  },
  server: {
    port: 5173,
    // P171: the Docs view bundles ../README.md, ../SECURITY.md, ../docs/*.md.
    fs: { allow: [path.resolve("..")] },
    proxy: {
      // Dev server: proxy API + WS to the relay
      "/ws": { target: RELAY.replace(/^http/, "ws"), ws: true, changeOrigin: true },
      "/agents": { target: RELAY, changeOrigin: true },
      "/sessions": { target: RELAY, changeOrigin: true },
      "/rooms": { target: RELAY, changeOrigin: true },
      "/listings": { target: RELAY, changeOrigin: true },
      "/events": { target: RELAY, changeOrigin: true },
      "/health": { target: RELAY, changeOrigin: true },
      "/payments": { target: RELAY, changeOrigin: true },
      "/messages": { target: RELAY, changeOrigin: true },
      "/agoras": { target: RELAY, changeOrigin: true },
      "/dm": { target: RELAY, changeOrigin: true },
      "/gaming": { target: RELAY, changeOrigin: true },
      "/safety": { target: RELAY, changeOrigin: true },
      "/federation": { target: RELAY, changeOrigin: true },
      "/ui-config": { target: RELAY, changeOrigin: true },
      // The gamify, results and operator reads (the Gamify tab said "the relay
      // did not answer" under `npm run dev` without these).
      ...Object.fromEntries(
        ["/relay", "/season", "/bugs", "/security", "/ratings", "/results", "/leaderboard",
          "/bounties", "/likes", "/operator", "/dashboard"]
          .map((p) => [p, { target: RELAY, changeOrigin: true }]),
      ),
    },
  },
  css: {
    modules: { localsConvention: "camelCase", generateScopedName: cssModuleName },
    postcss: { plugins: [noRemoteFonts] },
  },
});
