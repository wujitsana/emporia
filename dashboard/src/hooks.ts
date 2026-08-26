import { useEffect, useRef, useState } from "react";
import { resolveRelayUrl } from "./relayEnv";
import { dashboardToken } from "./api";

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

export interface RoomWsState {
  messages: WsMessage[];
  connected: boolean;
  send: (data: string) => void;
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
    const base = toWsUrl(relayUrl);

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
      const ws = new WebSocket(`${base}/ws/rooms/${roomId}?${params.toString()}`);
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
export interface RelayHealth {
  status: string; version: string; service: string;
  listing_count: number; session_count: number; modules: string[];
  guardrails_mode?: string;
  stripe_enabled?: boolean;
  operator_fee_bps?: number;
  // Present on GET /health; absent from the smaller WS "connected" frame snapshot
  // above (relay_server.py's ws_global handler sends only the first six fields).
  require_nous?: boolean;
  mpp_enabled?: boolean;
  payment_methods?: string[];
  stripe_profile_ready?: boolean;
  max_total_spend_cents?: number;
  stripe_mpp_admin_notice?: string | null;
}

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
    const RELAY = resolveRelayUrl();
    const base = toWsUrl(RELAY);
    let closed = false;
    let retryMs = 1_000;
    let ws: WebSocket;

    const connect = () => {
      if (closed) return;
      ws = new WebSocket(`${base}/ws/events`);

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
    };

    connect();
    const pingId = setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) ws.send('{"type":"ping"}');
    }, 20_000);

    return () => { closed = true; clearInterval(pingId); ws?.close(); };
  }, []);

  return { events, seed, wsConnected };
}
