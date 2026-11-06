/**
 * The chat's line to Hermes: the dashboard's own `/api/ws` socket, which speaks
 * the same newline-free JSON-RPC 2.0 dialect Hermes's terminal UI and desktop
 * app use (Hermes `tui_gateway`). Nothing here runs an agent: Hermes does.
 *
 * Frames:
 * - client → server: `{jsonrpc, id, method, params}`; the reply carries the same id.
 * - server → client events: `{method: "event", params: {type, session_id, payload, seq}}`.
 * - server → client requests (the agent asking the person, e.g. `approval`):
 *   `{id: "srq-…", method, params}`; answered with `{id, result}` or `{id, error}`.
 *
 * Profile scope: every call names `profile`, so a request can only reach the
 * Hermes profile the tab is open in (see docs/proposals/dashboard-chat.md).
 * The socket URL comes from the Hermes plugin SDK (`buildWsUrl`), which adds
 * Hermes's own credential; this module never sees, stores or shows it.
 */

export interface GatewayEvent {
  type: string;
  session_id?: string;
  payload?: Record<string, unknown>;
  seq?: number;
}

export interface ServerRequest {
  id: string;
  method: string;
  params: Record<string, unknown>;
}

type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void; timer: ReturnType<typeof setTimeout> };

export class RpcError extends Error {
  code: number;
  constructor(code: number, message: string) {
    super(message);
    this.code = code;
  }
}

/** JSON-RPC "method not found": how a client says it will not answer a request. */
export const METHOD_NOT_FOUND = -32601;

export type SocketFactory = () => Promise<WebSocket>;

export class ChatGateway {
  private ws: WebSocket | null = null;
  private seq = 0;
  private pending = new Map<string, Pending>();
  private eventHandlers = new Set<(e: GatewayEvent) => void>();
  private requestHandlers = new Set<(r: ServerRequest) => void>();
  private stateHandlers = new Set<(s: ChatConnState) => void>();
  state: ChatConnState = "idle";
  private readonly factory: SocketFactory;
  readonly profile: string;

  constructor(factory: SocketFactory, profile: string) {
    this.factory = factory;
    this.profile = profile;
  }

  onEvent(fn: (e: GatewayEvent) => void): () => void {
    this.eventHandlers.add(fn);
    return () => this.eventHandlers.delete(fn);
  }

  onRequest(fn: (r: ServerRequest) => void): () => void {
    this.requestHandlers.add(fn);
    return () => this.requestHandlers.delete(fn);
  }

  onState(fn: (s: ChatConnState) => void): () => void {
    this.stateHandlers.add(fn);
    return () => this.stateHandlers.delete(fn);
  }

  private setState(s: ChatConnState) {
    this.state = s;
    this.stateHandlers.forEach((fn) => fn(s));
  }

  async connect(): Promise<void> {
    if (this.ws && (this.state === "open" || this.state === "connecting")) return;
    this.setState("connecting");
    let ws: WebSocket;
    try {
      ws = await this.factory();
    } catch (e) {
      this.setState("closed");
      throw e;
    }
    this.ws = ws;
    await new Promise<void>((resolve, reject) => {
      ws.onopen = () => resolve();
      ws.onerror = () => reject(new Error("could not reach Hermes"));
      ws.onclose = () => reject(new Error("Hermes closed the connection"));
    });
    ws.onmessage = (m) => this.receive(typeof m.data === "string" ? m.data : "");
    ws.onclose = () => {
      this.ws = null;
      this.setState("closed");
      this.pending.forEach((p) => {
        clearTimeout(p.timer);
        p.reject(new Error("connection to Hermes closed"));
      });
      this.pending.clear();
    };
    ws.onerror = () => {};
    this.setState("open");
    // Say once that this client answers server→client requests (approval, clarify);
    // without it Hermes withdraws them instead of waiting.
    await this.call("client.capabilities", { server_requests: true }).catch(() => undefined);
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
  }

  /** Call a method. `profile` is always set to this tab's profile — a caller cannot widen it. */
  call<T = unknown>(method: string, params: Record<string, unknown> = {}, timeoutMs = 60_000): Promise<T> {
    const ws = this.ws;
    if (!ws || this.state !== "open") return Promise.reject(new Error("not connected to Hermes"));
    const id = `emp${++this.seq}`;
    const scoped = method === "client.capabilities" ? params : { ...params, profile: this.profile };
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Hermes did not answer ${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject, timer });
      ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params: scoped }));
    });
  }

  /** Answer a server→client request (an approval or a clarify). A write to Hermes, never to the relay. */
  respond(id: string, result: Record<string, unknown>): void {
    this.ws?.send(JSON.stringify({ jsonrpc: "2.0", id, result }));
  }

  /** Decline a server→client request this UI does not answer (a password, a secret). */
  decline(id: string, message: string): void {
    this.ws?.send(JSON.stringify({ jsonrpc: "2.0", id, error: { code: METHOD_NOT_FOUND, message } }));
  }

  /** Exposed for tests. */
  receive(raw: string): void {
    let frame: Record<string, unknown>;
    try {
      frame = JSON.parse(raw);
    } catch {
      return;
    }
    const id = frame.id;
    const method = frame.method;
    if (method === "event") {
      const params = frame.params as GatewayEvent | undefined;
      if (params && typeof params.type === "string") this.eventHandlers.forEach((fn) => fn(params));
      return;
    }
    if (typeof id === "string" && typeof method === "string") {
      const req = { id, method, params: (frame.params ?? {}) as Record<string, unknown> };
      if (this.requestHandlers.size === 0) this.decline(id, "no handler");
      else this.requestHandlers.forEach((fn) => fn(req));
      return;
    }
    if (typeof id === "string" && this.pending.has(id)) {
      const p = this.pending.get(id)!;
      this.pending.delete(id);
      clearTimeout(p.timer);
      const err = frame.error as { code?: number; message?: string } | undefined;
      if (err) p.reject(new RpcError(err.code ?? 0, err.message ?? "error"));
      else p.resolve(frame.result);
    }
  }
}

export type ChatConnState = "idle" | "connecting" | "open" | "closed";
