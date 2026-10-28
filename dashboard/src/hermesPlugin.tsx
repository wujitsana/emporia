/**
 * P126: the Emporia dashboard as a tab in the Hermes dashboard.
 *
 * Hermes loads this file as a classic script and renders the registered
 * component with *its* React (window.__HERMES_PLUGIN_SDK__.React). That
 * component only provides a host element; the Emporia app — with its own React
 * — renders into a *closed* shadow root on it, so:
 *
 * - its CSS (SRCL, themes) cannot leak into Hermes's page, nor Hermes's into it
 *   (selectors for html/body/:root were rewritten to the shadow host at build
 *   time — vite.plugin.config.ts);
 * - Hermes's scripts and other plugins cannot casually reach its DOM, which
 *   holds decrypted DMs.
 *
 * All requests go through the Hermes SDK (authedFetch, buildWsUrl), so they
 * carry Hermes's sign-in, to /api/plugins/emporia/* — plugin_api.py, which
 * proxies to the Emporia console. See docs/local-console.md.
 */
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { ensureViewer } from "./viewerBootstrap";

interface HermesPluginSdk {
  React: { createElement: (...a: unknown[]) => unknown };
  hooks: {
    useRef: <T>(v: T) => { current: T };
    useEffect: (fn: () => void | (() => void), deps: unknown[]) => void;
  };
  authedFetch: (url: string, init?: RequestInit) => Promise<Response>;
  buildWsUrl: (path: string, params?: Record<string, string>) => Promise<string>;
}

declare global {
  interface Window {
    __HERMES_PLUGIN_SDK__?: HermesPluginSdk;
    __HERMES_PLUGINS__?: { register: (name: string, component: unknown) => void };
  }
}

// P227: the Hermes profile this bundle was loaded for — Hermes tags each
// plugin script with it (data-hermes-profile) and reloads the bundle when the
// profile changes. Read now: currentScript is only set while this runs.
const HERMES_PROFILE = (document.currentScript as HTMLScriptElement | null)
  ?.getAttribute("data-hermes-profile") ?? "";

// The directory this script was served from: /dashboard-plugins/emporia/dist/.
const BASE = new URL(".", (document.currentScript as HTMLScriptElement | null)?.src
  ?? window.location.href).toString();

/** Fonts must be declared on the document — @font-face inside a shadow root is
 *  ignored — so only those rules are copied out, once, with absolute URLs. */
async function installFonts(css: string): Promise<void> {
  if (document.getElementById("emporia-plugin-fonts")) return;
  const faces = (css.match(/@font-face\s*{[^}]*}/g) ?? []).map((rule) =>
    rule.replace(/url\((['"]?)([^'")]+)\1\)/g, (_m, q, u) =>
      `url(${q}${new URL(u, BASE).toString()}${q})`));
  const style = document.createElement("style");
  style.id = "emporia-plugin-fonts";
  style.textContent = faces.join("\n");
  document.head.appendChild(style);
}

async function mount(host: HTMLElement, sdk: HermesPluginSdk): Promise<() => void> {
  const shadow = host.attachShadow({ mode: "closed" });
  const css = await fetch(new URL("style.css", BASE)).then((r) => r.text()).catch(() => "");
  await installFonts(css);
  const style = document.createElement("style");
  style.textContent = css;
  const container = document.createElement("div");
  container.className = "e-host-body";
  container.style.minHeight = "100%";
  shadow.append(style, container);

  window.__EMPORIA_HERMES_SDK__ = { authedFetch: sdk.authedFetch, buildWsUrl: sdk.buildWsUrl };
  await ensureViewer(
    () => sdk.authedFetch("/api/plugins/emporia/agents")
      .then((r) => r.json()).then((d) => d.agents as string[]),
    async () => HERMES_PROFILE || sdk.authedFetch("/api/plugins/emporia/_hermes_profile")
      .then((r) => r.json()).then((d) => String(d.profile ?? "")),
  );
  const { setThemeRoot } = await import("./themeConfig");
  setThemeRoot(container);
  const { default: App } = await import("./App.tsx");
  const root: Root = createRoot(container);
  root.render(<App />);
  return () => root.unmount();
}

(function register() {
  const sdk = window.__HERMES_PLUGIN_SDK__;
  if (!sdk || !window.__HERMES_PLUGINS__) return;
  const React = sdk.React;
  const { useRef, useEffect } = sdk.hooks;

  // A bundle Hermes loads once; fast refresh never applies.
  // oxlint-disable-next-line react/only-export-components
  function EmporiaTab() {
    const ref = useRef<HTMLDivElement | null>(null);
    useEffect(() => {
      let unmount: (() => void) | null = null;
      let gone = false;
      if (ref.current) {
        void mount(ref.current, sdk!).then((u) => (gone ? u() : (unmount = u)));
      }
      return () => { gone = true; unmount?.(); };
    }, []);
    return React.createElement("div", { ref, style: { height: "100%", minHeight: "80vh" } });
  }

  window.__HERMES_PLUGINS__.register("emporia", EmporiaTab);
})();
