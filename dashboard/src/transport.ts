/**
 * P126: how this dashboard reaches the relay — the one place that knows.
 *
 * - **relay**: served by a relay at /ui/ (or a dev server). Talks to the relay
 *   directly; identity is the X-Emporia-Agent-Id header or a dashboard JWT.
 * - **console**: served by the local console (`python -m emporia.console`).
 *   Everything goes through the console, which signs in to the relay as the
 *   agent being viewed with that agent's own key — so it works against any
 *   relay — and opens sealed DMs on this machine.
 * - **hermes**: the same console, reached through the Hermes dashboard's
 *   plugin API (`/api/plugins/emporia/…`), behind Hermes's sign-in. Requests
 *   go through the Hermes plugin SDK, which carries that sign-in.
 *
 * See docs/local-console.md.
 */
import { resolveRelayUrl, VIEWER_ID } from "./relayEnv";

export type Mode = "relay" | "console" | "hermes";

interface HermesSdk {
  authedFetch: (url: string, init?: RequestInit) => Promise<Response>;
  buildWsUrl: (path: string, params?: Record<string, string>) => Promise<string>;
}

declare global {
  interface Window {
    __EMPORIA_HERMES_SDK__?: HermesSdk;
  }
}

/** Set by the Hermes plugin entry before the app loads (hermesPlugin.tsx). */
function hermesSdk(): HermesSdk | null {
  return typeof window !== "undefined" ? window.__EMPORIA_HERMES_SDK__ ?? null : null;
}

export const MODE: Mode = hermesSdk()
  ? "hermes"
  : import.meta.env.VITE_EMPORIA_MODE === "console" ? "console" : "relay";

/** Whether the local console is in front: local agents, signed requests, DMs opened. */
export const VIA_CONSOLE = MODE !== "relay";

const PLUGIN_BASE = "/api/plugins/emporia";

/** The console's own endpoints (`/agents`, `/dm/<agent>/open`), by mode. */
export function consoleUrl(path: string): string {
  return (MODE === "hermes" ? PLUGIN_BASE : "") + path;
}

/** Base URL for relay REST calls. Via the console it names the viewer, whom the
 *  console signs in as; directly, it is the relay. */
export function relayHttpBase(viewer: string | null = VIEWER_ID): string {
  if (!VIA_CONSOLE) return resolveRelayUrl();
  return consoleUrl(`/relay/${encodeURIComponent(viewer ?? "")}`);
}

export function httpFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const sdk = hermesSdk();
  if (sdk) return sdk.authedFetch(url, init);
  return fetch(url, { credentials: "same-origin", ...init });
}

function wsOrigin(): string {
  const { protocol, host } = window.location;
  return `${protocol === "https:" ? "wss:" : "ws:"}//${host}`;
}

/**
 * Open a relay websocket (`/ws/agent/<id>`, `/ws/rooms/<id>`, `/ws/events`,
 * `/ws/<session>`). Async because the Hermes SDK mints a single-use ticket per
 * socket. Via the console, identity params are dropped here and added by the
 * console for the viewer.
 */
export async function relaySocket(path: string, params = new URLSearchParams()): Promise<WebSocket> {
  if (!VIA_CONSOLE) {
    const base = resolveRelayUrl().replace(/^http/, "ws").replace(/\/$/, "");
    const q = params.toString();
    return new WebSocket(`${base}${path}${q ? `?${q}` : ""}`);
  }
  params.delete("token");
  params.delete("agent_id");
  const viewer = encodeURIComponent(VIEWER_ID ?? "");
  const sdk = hermesSdk();
  if (sdk) {
    return new WebSocket(await sdk.buildWsUrl(`${PLUGIN_BASE}/ws/${viewer}${path}`,
      Object.fromEntries(params)));
  }
  const q = params.toString();
  return new WebSocket(`${wsOrigin()}/ws/${viewer}${path}${q ? `?${q}` : ""}`);
}
