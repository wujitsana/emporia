/**
 * P126: the Emporia dashboard as a tab in the Hermes dashboard.
 *
 * Hermes loads this file as a classic script and renders the registered
 * component with *its* React (window.__HERMES_PLUGIN_SDK__.React). That
 * component only provides a host element; the Emporia app — with its own React
 * — renders into a *closed* shadow root on it, so:
 *
 * - its CSS (the kit's, the themes') cannot leak into Hermes's page, nor Hermes's into it
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
import type { HermesProfile } from "./viewerBootstrap";

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
    __EMPORIA_HERMES_PROFILE__?: HermesProfile | null;
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

  // P296: a profile with no Emporia agent gets the one command that makes one,
  // not a console error.
  const probe = await sdk.authedFetch("/api/plugins/emporia/agents").catch(() => null);
  if (probe?.status === 503) {
    const d = await probe.json().catch(() => ({}));
    if (d.setup_needed) {
      container.innerHTML = "";
      container.append(notSetUp());
      return () => container.replaceChildren();
    }
  }
  window.__EMPORIA_HERMES_SDK__ = { authedFetch: sdk.authedFetch, buildWsUrl: sdk.buildWsUrl };
  window.__EMPORIA_HERMES_PROFILE__ = await ensureViewer(
    () => sdk.authedFetch("/api/plugins/emporia/agents")
      .then((r) => r.json()).then((d) => d.agents as string[]),
    // P234: never "" inside Hermes — that read as "not in Hermes" and kept the
    // last agent's view, DMs included, in the default profile.
    async () => HERMES_PROFILE || new URLSearchParams(window.location.search).get("profile")
      || await sdk.authedFetch("/api/plugins/emporia/_hermes_profile")
        .then((r) => r.json()).then((d) => String(d.profile ?? "")).catch(() => "")
      || "default",
  );
  const { setThemeRoot } = await import("./themeConfig");
  setThemeRoot(container);
  const { default: App } = await import("./App.tsx");
  const root: Root = createRoot(container);
  root.render(<App />);
  return () => root.unmount();
}

/** The tab in a profile Emporia is not set up in: what it is, and the command. */
function notSetUp(): HTMLElement {
  const box = document.createElement("div");
  box.style.cssText = "max-width:36rem;margin:3rem auto;padding:0 16px;font:15px/1.5 system-ui,sans-serif";
  const h = document.createElement("h2");
  h.textContent = "Emporia is not set up in this profile";
  const p = document.createElement("p");
  p.textContent = "Emporia is a marketplace where agents play, trade and pay each other. "
    + "To make an agent (in a new profile, or this one), run in a terminal:";
  const pre = document.createElement("pre");
  pre.style.cssText = "padding:.75rem 1rem;border-radius:6px;background:rgba(127,127,127,.15);overflow-x:auto";
  pre.textContent = "hermes emporia setup";
  const q = document.createElement("p");
  q.textContent = "Then open this tab in the agent's profile: hermes -p NAME dashboard.";
  box.append(h, p, pre, q);
  return box;
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
