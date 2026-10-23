import { useEffect, useRef, useState } from "react";
import { relaySocket } from "./transport";
import { dashboardToken } from "./api";
import type { Health } from "./api";

// ─── URL helpers ────────────────────────────────────────────────────────────

export function toWsUrl(httpUrl: string): string {
  try {
    const u = new URL(httpUrl);
    u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
    return u.origin + u.pathname.replace(/\/$/, "");
  } catch {
    return httpUrl.replace(/^https?:\/\//, (m) => (m.startsWith("https") ? "wss://" : "ws://"));
  }
}

// ─── Polling interval ────────────────────────────────────────────────────────

/**
 * Polls `fn` every `ms` milliseconds. Also re-polls immediately whenever
 * `deps` changes (use a version counter to force a refresh from outside).
 */
export function useInterval<T>(
  fn: () => Promise<T>,
  ms: number,
  deps: unknown[] = [],
): [T | null, boolean, string | null] {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    let mounted = true;
    const run = () =>
      fnRef
        .current()
        .then((d) => {
          if (mounted) { setData(d); setLoading(false); setError(null); }
        })
        .catch((e) => {
          if (mounted) { setError(String(e)); setLoading(false); }
        });
    run();
    const id = setInterval(run, ms);
    return () => { mounted = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return [data, loading, error];
}

// ─── Room WebSocket ──────────────────────────────────────────────────────────

export interface WsMessage {
  message_id: string;
  sender_id: string;
  msg_type: string;
  content: string;
  created_at: string;
  chain_hash?: string;
  /** P23-4: already sent by RoomMessage.to_dict() (asdict() serializes every
   *  dataclass field) -- previously typed here but unused past chain_hash;
   *  needed for client-side signature verification (integrityCheck.ts). */
  signature?: string | null;
  nonce?: string | null;
}

/**
 * A frame on the per-agent channel. Unlike WsMessage — which is one specific
 * shape, a room message — this channel multiplexes eight different event types,
 * so only `type` is guaranteed and the rest are per-event.
 */
export interface AgentEvent {
  type: string;
  thread_id?: string;
  room_id?: string;
  session_id?: string;
  message_id?: string;
  sender_id?: string;
  created_at?: string;
  [key: string]: unknown;
}

export interface RoomWsState {
  messages: WsMessage[];
  connected: boolean;
  send: (data: string) => void;
}

export interface AgentWsState {
  connected: boolean;
  /** The most recent event, with a fresh object identity per frame so an
   *  `useEffect(..., [last])` fires again on a repeat of the same event. */
  last: AgentEvent | null;
  /** Total events seen on this connection — useful as a dep when the payload
   *  itself doesn't matter. */
  eventCount: number;
}

/**
 * Connects to /ws/agent/{agentId} — the relay's per-agent notification channel.
 *
 * P38-1: this channel has been fully wired server-side all along. It emits
 * `dm_received` (with content), `session_created`, `session_started`,
 * `room_invite`, `room_message`, `kicked_from_room`, `agora_invite` and
 * `agora_post_created` — and the dashboard subscribed to none of it, polling
 * DMs every 4s and the inbox every 10s instead. For a dashboard whose whole job
 * is showing what the agent is doing, that was the largest visibility gap left.
 *
 * Deliberately exposes events rather than merging them into state: callers
 * refetch the endpoint they already trust on the events they care about. That
 * keeps one source of truth for the data and leaves the socket responsible only
 * for *when* — no ordering, dedup or partial-frame handling to get wrong. The
 * existing polls stay as a slow safety net for a dropped socket.
 */
export function useAgentWs(relayUrl: string, agentId: string | null): AgentWsState {
  const [connected, setConnected] = useState(false);
  const [last, setLast] = useState<AgentEvent | null>(null);
  const [eventCount, setEventCount] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    setConnected(false);
    setLast(null);
    setEventCount(0);
    if (!agentId) return;

    let closed = false;
    let retryMs = 1_000;

    const connect = () => {
      if (closed) return;
      const params = new URLSearchParams({ agent_id: agentId });
      const token = dashboardToken();
      if (token) params.set("token", token);
      void relaySocket(`/ws/agent/${encodeURIComponent(agentId)}`, params).then((ws) => {
      if (closed) { ws.close(); return; }
      wsRef.current = ws;

      ws.onopen = () => { setConnected(true); retryMs = 1_000; };
      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        if (!closed) setTimeout(connect, (retryMs = Math.min(retryMs * 2, 30_000)));
      };
      ws.onmessage = (e) => {
        let frame: AgentEvent;
        try {
          frame = JSON.parse(e.data);
        } catch {
          return;
        }
        // The keepalive is not an event; counting it would refetch every 20s
        // and quietly reinstate the polling this hook exists to remove.
        if (!frame?.type || frame.type === "pong") return;
        setLast({ ...frame });
        setEventCount((n) => n + 1);
      };
      }).catch(() => { if (!closed) setTimeout(connect, (retryMs = Math.min(retryMs * 2, 30_000))); });
    };

    connect();
    const pingId = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN)
        wsRef.current.send('{"type":"ping"}');
    }, 20_000);

    return () => {
      closed = true;
      clearInterval(pingId);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [relayUrl, agentId]);

  return { connected, last, eventCount };
}

/**
 * Connects to /ws/rooms/{roomId}. On connect the relay sends room_init with
 * the last 20 messages; subsequent room_message frames are appended. Reconnects
 * with exponential back-off on drop.
 */
export function useRoomWs(
  relayUrl: string,
  roomId: string | null,
  agentId: string | null,
): RoomWsState {
  const [messages, setMessages] = useState<WsMessage[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    // P24-5 (W5, F2): clear unconditionally on every roomId change, not just
    // the falsy transition -- otherwise a room->room switch renders the old
    // room's messages against the new room's verification context until the
    // new room's room_init frame arrives, producing a false "unverified" chip.
    setMessages([]);
    setConnected(false);

    if (!roomId) {
      return;
    }

    let closed = false;
    let retryMs = 1_000;

    const connect = () => {
      if (closed) return;
      // agent_id is trusted only for co-located (localhost) connections; a remote relay
      // authenticates private-room access via the dashboard JWT instead — see
      // relay_server.py's _ws_agent_id().
      // P35-1: a spectator has no agent_id; omit the param rather than send a
      // placeholder the relay would have to interpret.
      const params = new URLSearchParams();
      if (agentId) params.set("agent_id", agentId);
      const token = dashboardToken();
      if (token) params.set("token", token);
      void relaySocket(`/ws/rooms/${roomId}`, params).then((ws) => {
      if (closed) { ws.close(); return; }
      wsRef.current = ws;

      ws.onopen = () => { setConnected(true); retryMs = 1_000; };
      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        if (!closed) setTimeout(connect, (retryMs = Math.min(retryMs * 2, 30_000)));
      };
      ws.onmessage = (e) => {
        const frame = JSON.parse(e.data);
        if (frame.type === "room_init") {
          // relay sends full recent history on connect — use as source of truth
          setMessages(frame.messages ?? []);
        } else if (frame.type === "room_message") {
          setMessages((prev) => [...prev, frame]);
        }
      };
      }).catch(() => { if (!closed) setTimeout(connect, (retryMs = Math.min(retryMs * 2, 30_000))); });
    };

    connect();
    const pingId = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN)
        wsRef.current.send('{"type":"ping"}');
    }, 20_000);

    return () => {
      closed = true;
      clearInterval(pingId);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [relayUrl, roomId, agentId]);

  const send = (data: string) => wsRef.current?.send(data);
  return { messages, connected, send };
}

// ─── Sender pubkeys ──────────────────────────────────────────────────────────

/**
 * Fetches and caches each distinct sender's pubkey, for verifyMessageSignature().
 *
 * P24-5 (W5, F3): RoomsView used to key this effect on `[messages, pubkeys]`
 * with `messages` a WS-frame array that gets a new identity on every frame --
 * the effect re-ran constantly, and a second run before the first's fetch
 * resolved could re-request the same id (no infinite loop; failures cache
 * "", so it self-limits, but requests were still unbounded in flight).
 * Fixed by keying on a stable, deduped, sorted senderKey string instead of
 * the raw id array, and tracking in-flight ids in a ref so a re-run before
 * the first request lands never re-requests the same id.
 */
export function useSenderPubkeys(
  senderIds: string[],
  fetchPubkey: (agentId: string) => Promise<string>,
): Record<string, string> {
  const [pubkeys, setPubkeys] = useState<Record<string, string>>({});
  const pubkeysRef = useRef(pubkeys);
  pubkeysRef.current = pubkeys;
  const requestedRef = useRef<Set<string>>(new Set());
  const fetchRef = useRef(fetchPubkey);
  fetchRef.current = fetchPubkey;
  // A request for id X, once started, must be allowed to land no matter which
  // later effect run's closure it resolves in -- requestedRef's dedup is only
  // sound if a landed result is never dropped, so unmount (not "this effect
  // run got superseded") is the only reason to discard it.
  const unmountedRef = useRef(false);
  useEffect(() => () => { unmountedRef.current = true; }, []);

  const senderKey = [...new Set(senderIds)].sort().join(" ");

  useEffect(() => {
    const ids = senderKey ? senderKey.split(" ") : [];
    const unknown = ids.filter((id) => !(id in pubkeysRef.current) && !requestedRef.current.has(id));
    if (unknown.length === 0) return;
    unknown.forEach((id) => requestedRef.current.add(id));

    Promise.all(unknown.map((id) => fetchRef.current(id).then((key) => [id, key] as const)))
      .then((pairs) => {
        if (unmountedRef.current) return;
        setPubkeys((prev) => ({ ...prev, ...Object.fromEntries(pairs) }));
      });
  }, [senderKey]);

  return pubkeys;
}

// ─── Global event stream ─────────────────────────────────────────────────────

export interface GlobalEvent { type: string; _ts: number; [k: string]: unknown }
/**
 * The `GET /health` payload.
 *
 * P37-1: this was a second, hand-maintained description of the same response
 * that `api.Health` already described — 14 fields against 19, overlapping but
 * not equal. Adding `stripe_mode` to one of them and not the other is exactly
 * how that ends, and it is how it ended: the compiler caught it only because a
 * caller happened to cross from one type to the other.
 *
 * The WS "connected" frame carries a subset of these (relay_server.py's
 * ws_global handler sends the first six), which is why so many fields are
 * optional — a subset is a reason for optionality, not for a second type.
 */
export type RelayHealth = Health;

export interface GlobalWsState {
  events: GlobalEvent[];
  seed: RelayHealth | null;   // health snapshot from the initial `connected` frame
  wsConnected: boolean;
}

/**
 * Connects to /ws/events and delivers all relay-side events to the dashboard.
 * The relay sends a `connected` frame on connect with a health snapshot — used
 * to seed the Overview instantly before the first HTTP poll completes.
 */
export function useGlobalEvents(): GlobalWsState {
  const [events, setEvents] = useState<GlobalEvent[]>([]);
  const [seed, setSeed] = useState<RelayHealth | null>(null);
  const [wsConnected, setWsConnected] = useState(false);

  useEffect(() => {
    let closed = false;
    let retryMs = 1_000;
    let ws: WebSocket;

    const connect = () => {
      if (closed) return;
      void relaySocket("/ws/events").then((sock) => {
      if (closed) { sock.close(); return; }
      ws = sock;

      ws.onopen = () => { setWsConnected(true); retryMs = 1_000; };
      ws.onclose = () => {
        setWsConnected(false);
        if (!closed) setTimeout(connect, (retryMs = Math.min(retryMs * 2, 30_000)));
      };
      ws.onmessage = (e) => {
        const f = JSON.parse(e.data);
        if (f.type === "pong") return;
        if (f.type === "connected") { setSeed(f.health); return; }
        setEvents((prev) => [{ ...f, _ts: Date.now() }, ...prev].slice(0, 200));
      };
      }).catch(() => { if (!closed) setTimeout(connect, (retryMs = Math.min(retryMs * 2, 30_000))); });
    };

    connect();
    const pingId = setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) ws.send('{"type":"ping"}');
    }, 20_000);

    return () => { closed = true; clearInterval(pingId); ws?.close(); };
  }, []);

  return { events, seed, wsConnected };
}
