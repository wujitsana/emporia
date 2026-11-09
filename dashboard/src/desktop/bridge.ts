/**
 * The Hermes Desktop page's door to the host: the three plugin-context members
 * the shared app needs, handed over by the entry (desktop/plugin.tsx) at
 * register time. Nothing here imports the SDK, so the shared modules that read
 * it (transport.ts, prefs.ts) build the same way for every target.
 *
 * - `rest` is `ctx.rest`: JSON to `/api/plugins/emporia/<path>`, with
 *   Desktop's own sign-in and profile routing. The plugin never holds a token.
 * - `socket` is `ctx.socket`: receive-only frames from the same namespace; a
 *   no-op where Desktop cannot carry a plugin socket (OAuth remotes, the
 *   browser Webapp), so every consumer keeps a polling fallback.
 * - `storage` is `ctx.storage`: preferences namespaced to this plugin.
 * - `navigate` is `host.navigate`: another Desktop page, for links out of the app.
 */

export interface DesktopRestOptions {
  method?: string;
  body?: unknown;
  timeoutMs?: number;
}

export interface DesktopStorage {
  get<T>(key: string, fallback: T): T;
  set(key: string, value: unknown): void;
  remove(key: string): void;
}

export interface DesktopBridge {
  rest: <T>(path: string, opts?: DesktopRestOptions) => Promise<T>;
  socket: (path: string, onMessage: (data: unknown) => void) => () => void;
  storage?: DesktopStorage;
  /** `host.navigate`: open another Desktop page (Settings links to Messaging, where Hermes edits them). */
  navigate?: (path: string) => void;
}

let current: DesktopBridge | null = null;

export function setDesktopBridge(bridge: DesktopBridge | null): void {
  current = bridge;
}

export function desktopBridge(): DesktopBridge | null {
  return current;
}
