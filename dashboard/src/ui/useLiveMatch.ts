import { useCallback, useEffect, useState } from "react";
import { api, dashboardToken } from "../api";
import type { Session, SessionAction } from "../api";
import { sessionIsActive } from "../chessReplay";
import { VIEWER_ID } from "../relayEnv";
import { relaySocket } from "../transport";

/**
 * A match, live: its action log and its session, kept current by the relay's socket for the match
 * (`/ws/<id>`), or — where there is no socket — by asking every few seconds. Read only.
 */
export function useLiveMatch(initial: Session) {
  const id = initial.session_id;
  const [session, setSession] = useState<Session>(initial);
  const [actions, setActions] = useState<SessionAction[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const [wsLive, setWsLive] = useState(false);

  const refresh = useCallback(() => {
    api.sessionActions(id).then((r) => { setActions(r.actions); setLoaded(true); setFailed(false); })
      .catch(() => { setLoaded(true); setFailed(true); });
    api.session(id).then(setSession).catch(() => {});
  }, [id]);

  useEffect(() => { refresh(); }, [refresh]);

  const active = sessionIsActive(session.status) || session.status === "waiting";
  useEffect(() => {
    if (!active || wsLive) return;
    const t = setInterval(refresh, 5_000);
    return () => clearInterval(t);
  }, [active, wsLive, refresh]);

  useEffect(() => {
    if (!active) return;
    let closed = false;
    let retryMs = 1_000;
    let ws: WebSocket | undefined;
    const connect = () => {
      if (closed) return;
      const params = new URLSearchParams();
      if (VIEWER_ID) params.set("agent_id", VIEWER_ID);
      const token = dashboardToken();
      if (token) params.set("token", token);
      void relaySocket(`/ws/${id}`, params).then((sock) => {
        if (closed) { sock.close(); return; }
        ws = sock;
        ws.onopen = () => { setWsLive(true); retryMs = 1_000; };
        ws.onclose = () => {
          setWsLive(false);
          if (!closed) setTimeout(connect, (retryMs = Math.min(retryMs * 2, 30_000)));
        };
        ws.onmessage = (e) => {
          try {
            const f = JSON.parse(e.data);
            if (f.type !== "pong" && f.type !== "ping") refresh();
          } catch { /* a frame that is not JSON is not ours */ }
        };
      }).catch(() => { if (!closed) setTimeout(connect, (retryMs = Math.min(retryMs * 2, 30_000))); });
    };
    connect();
    const ping = setInterval(() => { if (ws?.readyState === WebSocket.OPEN) ws.send('{"type":"ping"}'); }, 20_000);
    return () => { closed = true; clearInterval(ping); ws?.close(); };
  }, [id, active, refresh]);

  return { session, actions, loaded, failed, wsLive };
}
