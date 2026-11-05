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
 * - **desktop**: the same plugin API, from the Hermes Desktop page (and the
 *   browser Webapp, which runs the same renderer). Requests go through
 *   `ctx.rest`, which carries Desktop's sign-in and profile; sockets through
 *   `ctx.socket` (desktop/restShim.ts). Fixed at build time
 *   (vite.desktop.config.ts), because the page is one module evaluated once.
 *
 * See docs/local-console.md.
 */
import { resolveRelayUrl, VIEWER_ID } from "./relayEnv";
import { desktopBridge } from "./desktop/bridge";
import { PLUGIN_BASE, PluginSocket, restFetch } from "./desktop/restShim";

export type Mode = "relay" | "console" | "hermes" | "desktop";

interface HermesSdk {
  authedFetch: (url: string, init?: RequestInit) => Promise<Response>;
  buildWsUrl: (path: string, params?: Record<string, string>) => Promise<string>;
  /** Whether Hermes carries this plugin's websockets (plugin_api.py `sockets`). False when the
   *  profile's plugin runs in its plugin host, which answers every socket with a 501. */
  sockets?: boolean;
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

export const MODE: Mode = import.meta.env.VITE_EMPORIA_MODE === "desktop"
  ? "desktop"
  : hermesSdk()
    ? "hermes"
    : import.meta.env.VITE_EMPORIA_MODE === "console" ? "console" : "relay";

/** Inside a Hermes host (the dashboard tab or the Desktop page): the app is the profile's. */
export const IN_HERMES = MODE === "hermes" || MODE === "desktop";

export interface HostProfile {
  name: string;
  isAgent: boolean;
}

let desktopProfile: HostProfile | null = null;

/** The Desktop page sets the profile it is showing (desktop/mount.tsx). */
export function setDesktopProfile(p: HostProfile | null): void {
  desktopProfile = p;
}

/** P231: inside Hermes, the Hermes profile the tab or page is open in — the
 *  app views as that profile's agent and offers no switcher. Null elsewhere. */
export function hermesProfile(): HostProfile | null {
  if (MODE === "desktop") return desktopProfile;
  return typeof window !== "undefined"
    ? (window as { __EMPORIA_HERMES_PROFILE__?: HostProfile | null }).__EMPORIA_HERMES_PROFILE__ ?? null
    : null;
}

/** Whether the local console is in front: local agents, signed requests, DMs opened. */
export const VIA_CONSOLE = MODE !== "relay";

/** The console's own endpoints (`/agents`, `/dm/<agent>/open`), by mode. */
export function consoleUrl(path: string): string {
  return (IN_HERMES ? PLUGIN_BASE : "") + path;
}

/** Base URL for relay REST calls. Via the console it names the viewer, whom the
 *  console signs in as; directly, it is the relay. */
export function relayHttpBase(viewer: string | null = VIEWER_ID): string {
  if (!VIA_CONSOLE) return resolveRelayUrl();
  return consoleUrl(`/relay/${encodeURIComponent(viewer ?? "")}`);
}

export function httpFetch(url: string, init: RequestInit = {}): Promise<Response> {
  if (MODE === "desktop") {
    const bridge = desktopBridge();
    return bridge ? restFetch(bridge, url, init) : Promise.reject(new Error("the Desktop plugin is not loaded"));
  }
  const sdk = hermesSdk();
  if (sdk) return sdk.authedFetch(url, init);
  return fetch(url, { credentials: "same-origin", ...init });
}

/**
 * Whether relay sockets can be opened here. Only in the Hermes tab can they not: when the tab's
 * profile is not the one its dashboard was launched as (or `plugins.isolation: host`), Hermes
 * runs the plugin in that profile's plugin host, which carries plain requests only. It refuses
 * each socket with an HTTP 501, and uvicorn logs every refusal as "ASGI callable returned
 * without completing handshake" — once per socket and per retry.
 */
export function socketsCarried(): boolean {
  return MODE !== "hermes" || hermesSdk()?.sockets !== false;
}

/**
 * The socket handed out where none can be opened: it never opens, never closes and sends
 * nothing, so the hooks that use it neither show "live" nor dial again. Their REST polls,
 * which run anyway as the safety net for a dropped socket, keep the views current.
 */
export class QuietSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 3;
  readyState = QuietSocket.CONNECTING;
  onopen: ((e: Event) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  send(_data: string): void {
    void _data;
  }
  close(): void {
    this.readyState = QuietSocket.CLOSED;
  }
}

function wsOrigin(): string {
  const { protocol, host } = window.location;
  return `${protocol === "https:" ? "wss:" : "ws:"}//${host}`;
}

/**
 * Open a relay websocket (`/ws/agent/<id>`, `/ws/rooms/<id>`, `/ws/events`,
 * `/ws/<session>`). Async because the Hermes SDK mints a single-use ticket per
 * socket. Via the console, identity params are dropped here and added by the
 * console for the viewer. On Desktop the socket only receives (see
 * desktop/restShim.ts PluginSocket).
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
  const q = params.toString();
  if (MODE === "desktop") {
    const bridge = desktopBridge();
    if (!bridge) throw new Error("the Desktop plugin is not loaded");
    return new PluginSocket(bridge, `/ws/${viewer}${path}${q ? `?${q}` : ""}`) as unknown as WebSocket;
  }
  const sdk = hermesSdk();
  if (sdk && sdk.sockets === false) return new QuietSocket() as unknown as WebSocket;
  if (sdk) {
    return new WebSocket(await sdk.buildWsUrl(`${PLUGIN_BASE}/ws/${viewer}${path}`,
      Object.fromEntries(params)));
  }
  return new WebSocket(`${wsOrigin()}/ws/${viewer}${path}${q ? `?${q}` : ""}`);
}
