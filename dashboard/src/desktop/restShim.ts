/**
 * The shared app speaks `fetch` and `WebSocket`; Hermes Desktop gives a plugin
 * `ctx.rest` (JSON in, JSON out, throws on HTTP >= 400) and `ctx.socket`
 * (receive-only frames). These two shims turn one into the other, so no view
 * knows which host it runs in.
 */
import type { DesktopBridge } from "./bridge";

/** Where the plugin's backend (dashboard/plugin_api.py) is mounted. */
export const PLUGIN_BASE = "/api/plugins/emporia";

function jsonResponse(status: number, text: string): Response {
  return new Response(text, {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

/**
 * The HTTP status a failed `ctx.rest` call carries, and the body after it, or
 * null for a failure with no status (timeout, no bridge, HTML for JSON). The
 * main process sets `statusCode`; its message is "<status>: <body>", and across
 * IPC only the message survives, behind Electron's own prefix. The Webapp
 * throws the same "<status>: <body>" message.
 */
export function statusOf(e: unknown): { status: number; body: string } | null {
  const message = e instanceof Error ? e.message : String(e ?? "");
  const m = /\b([45]\d\d): ([\s\S]*)$/.exec(message);
  const coded = Number((e as { statusCode?: unknown } | null)?.statusCode);
  const status = Number.isInteger(coded) && coded >= 400 && coded <= 599 ? coded : m ? Number(m[1]) : NaN;
  if (!Number.isFinite(status)) return null;
  return { status, body: m && Number(m[1]) === status ? m[2] : JSON.stringify({ detail: message }) };
}

function requestBody(body: BodyInit | null | undefined): unknown {
  if (body === undefined || body === null) return undefined;
  if (typeof body !== "string") throw new TypeError("the Desktop page sends JSON bodies only");
  try {
    return JSON.parse(body);
  } catch {
    throw new TypeError("the Desktop page sends JSON bodies only");
  }
}

/**
 * `fetch(url, init)` over `ctx.rest`. Only URLs in the plugin's own namespace
 * exist here; anything else is a 404, never a request. A 2xx answer becomes a
 * 200 with its JSON; an HTTP error becomes that status with its body, so the
 * 503 `{"setup_needed": true}` reads exactly as it does in the tab. A failure
 * with no status rejects, as a network error rejects `fetch`.
 */
export async function restFetch(bridge: DesktopBridge, url: string, init: RequestInit = {}): Promise<Response> {
  if (!url.startsWith(`${PLUGIN_BASE}/`)) {
    return jsonResponse(404, JSON.stringify({ detail: `not in the plugin's namespace: ${url}` }));
  }
  const path = url.slice(PLUGIN_BASE.length);
  const method = (init.method ?? "GET").toUpperCase();
  const body = requestBody(init.body);
  try {
    const data = await bridge.rest<unknown>(path, body === undefined ? { method } : { method, body });
    return jsonResponse(200, JSON.stringify(data ?? null));
  } catch (e) {
    const failed = statusOf(e);
    if (!failed) throw e;
    return jsonResponse(failed.status, failed.body);
  }
}

/**
 * The part of `WebSocket` the app uses, over `ctx.socket`. That door has no
 * open or close events and cannot send, so:
 *
 * - it reads as open from its first frame (the relay sends one on connect on
 *   the events and room channels; the agent and game channels open on their
 *   first event, and their REST polls cover the time before);
 * - `send` drops the frame: the relay's keepalive pings are not needed, the
 *   server side keeps the socket alive at the protocol level;
 * - `ctx.socket` reconnects by itself, so this never fires `onclose`, and
 *   `close()` ends it for good.
 */
export class PluginSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 3;

  readyState = PluginSocket.CONNECTING;
  onopen: ((e: Event) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  private readonly dispose: () => void;

  constructor(bridge: DesktopBridge, path: string) {
    this.dispose = bridge.socket(path, (data) => {
      if (this.readyState === PluginSocket.CLOSED) return;
      if (this.readyState !== PluginSocket.OPEN) {
        this.readyState = PluginSocket.OPEN;
        this.onopen?.(new Event("open"));
      }
      this.onmessage?.(new MessageEvent("message", { data: typeof data === "string" ? data : JSON.stringify(data) }));
    });
  }

  send(_data: string): void {
    void _data;
  }

  close(): void {
    if (this.readyState === PluginSocket.CLOSED) return;
    this.readyState = PluginSocket.CLOSED;
    this.dispose();
  }
}
