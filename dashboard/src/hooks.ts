import { useEffect, useRef, useState } from "react";
import { resolveRelayUrl } from "./relayEnv";

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
  agentId: string,
): RoomWsState {
  const [messages, setMessages] = useState<WsMessage[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!roomId) {
      setMessages([]);
      setConnected(false);
      return;
    }

    let closed = false;
    let retryMs = 1_000;
    const base = toWsUrl(relayUrl);

    const connect = () => {
      if (closed) return;
      const ws = new WebSocket(`${base}/ws/rooms/${roomId}?agent_id=${encodeURIComponent(agentId)}`);
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

// ─── Global event stream ─────────────────────────────────────────────────────

export interface GlobalEvent { type: string; _ts: number; [k: string]: unknown }
export interface RelayHealth {
  status: string; version: string; service: string;
  listing_count: number; session_count: number; modules: string[];
  guardrails_mode?: string;
  stripe_enabled?: boolean;
  operator_fee_bps?: number;
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
