import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

const RELAY = process.env.VITE_RELAY_URL ?? "http://127.0.0.1:8088";
const SRCL = path.resolve("node_modules/srcl");

// When VITE_RELAY_URL is empty the built app uses relative URLs — correct
// when the relay itself serves the dashboard at /ui/.
const isEmbedded = !process.env.VITE_RELAY_URL;

export default defineConfig({
  base: isEmbedded ? "/ui/" : "/",
  plugins: [react()],
  resolve: {
    alias: {
      "@components": path.join(SRCL, "components"),
      "@common": path.join(SRCL, "common"),
      "@modules": path.join(SRCL, "modules"),
    },
  },
  server: {
    port: 5173,
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
      "/ptgs": { target: RELAY, changeOrigin: true },
      "/ui-config": { target: RELAY, changeOrigin: true },
    },
  },
  css: { modules: { localsConvention: "camelCase" } },
});
