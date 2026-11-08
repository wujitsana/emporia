/**
 * The chat's state: one Hermes socket for the tab's profile, the current
 * session, its transcript, the profile's session list and the command catalog.
 *
 * Every write here goes to Hermes — a prompt, a slash command, an interrupt,
 * an answer to an approval — never to the relay. The agent acts on the relay
 * with its own tools and its own guards (playbook, approvals, key_guard, the
 * signer's limits); the dashboard signs nothing.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ANSWERED_REQUESTS,
  EMPTY,
  applyEvent,
  applyRequest,
  fromTranscript,
  markAnswered,
  nextId,
  type ChatItem,
  type ChatState,
} from "./chatModel";
import { classify, completionRows, parseCatalog, reachNote, splitCommand, type Catalog, type CompletionRow } from "./commands";
import type { ChatConnState, ChatGateway, GatewayEvent, ServerRequest } from "./gateway";
import { redactSecrets } from "./redact";

export interface SessionRow {
  id: string;
  title: string;
  preview: string;
  startedAt: number | null;
  messages: number;
  source: string;
  /** A session the Emporia platform feeds (relay events): read-only here. */
  relay: boolean;
}

export interface Current {
  /** Hermes's runtime id for the live session; null before the first message. */
  sid: string | null;
  /** The stored id (what the session list shows). */
  stored: string | null;
  /** A relay session opened read-only. */
  readOnly: boolean;
}

/** Sessions the Emporia platform adapter writes (hermes_plugin/emporia/adapter.py). */
export const RELAY_SOURCE = "emporia";
/** What this chat stamps on the sessions it starts. */
export const CHAT_SOURCE = "dashboard";

export function toRow(r: Record<string, unknown>): SessionRow {
  const source = String(r.source ?? "");
  return {
    id: String(r.resolved_id ?? r.id ?? ""),
    title: String(r.title ?? "") || "untitled",
    preview: redactSecrets(String(r.preview ?? "")),
    startedAt: typeof r.started_at === "number" ? r.started_at : null,
    messages: typeof r.message_count === "number" ? r.message_count : 0,
    source,
    relay: source === RELAY_SOURCE,
  };
}

type Fetcher = (url: string) => Promise<Response>;

export function useChat(gw: ChatGateway | null, fetcher?: Fetcher) {
  const [conn, setConn] = useState<ChatConnState>(gw?.state ?? "idle");
  const [chat, setChat] = useState<ChatState>(EMPTY);
  const [current, setCurrent] = useState<Current>({ sid: null, stored: null, readOnly: false });
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [sessionsError, setSessionsError] = useState("");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [model, setModel] = useState("");
  const [error, setError] = useState("");
  const sidRef = useRef<string | null>(null);
  const lastUserText = useRef("");

  const push = useCallback((item: ChatItem) => setChat((s) => ({ ...s, items: [...s.items, item] })), []);
  const notice = useCallback(
    (text: string, tone: "info" | "error" | "command" = "info") => push({ kind: "notice", id: nextId("n"), text: redactSecrets(text), tone }),
    [push],
  );

  const refreshSessions = useCallback(async () => {
    if (!gw || gw.state !== "open") return;
    try {
      const r = await gw.call<{ sessions?: Record<string, unknown>[] }>("session.list", { limit: 50 });
      setSessions((r.sessions ?? []).map(toRow).filter((s) => s.id));
      setSessionsError("");
    } catch (e) {
      setSessionsError((e as Error).message);
    }
  }, [gw]);

  useEffect(() => {
    if (!gw) return;
    const offState = gw.onState(setConn);
    const offEvent = gw.onEvent((ev: GatewayEvent) => {
      if (ev.type === "sessions.changed") {
        void refreshSessions();
        return;
      }
      if (!ev.session_id || ev.session_id !== sidRef.current) return;
      if (ev.type === "session.info" && typeof ev.payload?.model === "string") setModel(ev.payload.model);
      setChat((s) => applyEvent(s, ev.type, ev.payload ?? {}));
    });
    const offReq = gw.onRequest((req: ServerRequest) => {
      const sid = String(req.params.session_id ?? "");
      if (sid && sid !== sidRef.current) {
        gw.decline(req.id, "not the session open in the Emporia tab");
        return;
      }
      if (!ANSWERED_REQUESTS.has(req.method)) gw.decline(req.id, "the Emporia tab does not answer this; use Hermes's own chat");
      setChat((s) => applyRequest(s, req.id, req.method, req.params));
    });
    return () => {
      offState();
      offEvent();
      offReq();
    };
  }, [gw, refreshSessions]);

  const connect = useCallback(async () => {
    if (!gw) return;
    if (gw.state === "open" || gw.state === "connecting") return;
    setError("");
    try {
      await gw.connect();
      const [cat] = await Promise.all([gw.call("commands.catalog", {}).catch(() => null), refreshSessions()]);
      if (cat) setCatalog(parseCatalog(cat));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [gw, refreshSessions]);

  const ensureSession = useCallback(async (): Promise<string> => {
    if (!gw) throw new Error("no chat here");
    if (sidRef.current) return sidRef.current;
    const r = await gw.call<{ session_id: string; stored_session_id: string; info?: Record<string, unknown> }>(
      "session.create",
      { source: CHAT_SOURCE, close_on_disconnect: true },
    );
    const answered = String(r.info?.profile_name ?? "");
    if (answered && answered !== gw.profile) {
      // Never chat as another profile: close what Hermes opened and stop.
      void gw.call("session.close", { session_id: r.session_id }).catch(() => undefined);
      throw new Error(`Hermes opened a session for profile "${answered}", not "${gw.profile}"; refused`);
    }
    if (typeof r.info?.model === "string") setModel(r.info.model);
    sidRef.current = r.session_id;
    setCurrent({ sid: r.session_id, stored: r.stored_session_id, readOnly: false });
    void refreshSessions();
    return r.session_id;
  }, [gw, refreshSessions]);

  const submit = useCallback(
    async (text: string, shown?: string) => {
      if (!gw) return;
      const sid = await ensureSession();
      lastUserText.current = text;
      setChat((s) => ({ ...s, busy: true, items: [...s.items, { kind: "user", id: nextId("u"), text: shown ?? text }] }));
      try {
        await gw.call("prompt.submit", { session_id: sid, text });
      } catch (e) {
        setChat((s) => ({ ...s, busy: false }));
        notice((e as Error).message, "error");
      }
    },
    [gw, ensureSession, notice],
  );

  const newSession = useCallback(() => {
    const old = sidRef.current;
    if (gw && old) void gw.call("session.close", { session_id: old }).catch(() => undefined);
    sidRef.current = null;
    setCurrent({ sid: null, stored: null, readOnly: false });
    setChat(EMPTY);
  }, [gw]);

  const interrupt = useCallback(async () => {
    if (!gw || !sidRef.current) return;
    await gw.call("session.interrupt", { session_id: sidRef.current }).catch((e) => notice((e as Error).message, "error"));
  }, [gw, notice]);

  const openSession = useCallback(
    async (row: SessionRow) => {
      if (!gw) return;
      const old = sidRef.current;
      if (old) void gw.call("session.close", { session_id: old }).catch(() => undefined);
      sidRef.current = null;
      if (row.relay) {
        // Read-only: a relay session is the Emporia platform's; this chat never writes into it.
        setCurrent({ sid: null, stored: row.id, readOnly: true });
        setChat({ ...EMPTY, title: row.title });
        try {
          const res = await (fetcher ?? fetch)(
            `/api/sessions/${encodeURIComponent(row.id)}/messages?profile=${encodeURIComponent(gw.profile)}`,
          );
          if (!res.ok) throw new Error(`Hermes answered ${res.status}`);
          const d = (await res.json()) as { messages?: Record<string, unknown>[] };
          setChat({ ...EMPTY, title: row.title, items: fromTranscript(d.messages ?? [], { relaySession: true }) });
        } catch (e) {
          notice((e as Error).message, "error");
        }
        return;
      }
      try {
        const r = await gw.call<{
          session_id: string;
          stored_session_id?: string | null;
          messages?: Record<string, unknown>[];
          info?: Record<string, unknown>;
          running?: boolean | null;
          open_requests?: { id: string; method: string; params: Record<string, unknown> }[] | null;
        }>("session.resume", { session_id: row.id, close_on_disconnect: true }, 90_000);
        sidRef.current = r.session_id;
        if (typeof r.info?.model === "string") setModel(r.info.model);
        setCurrent({ sid: r.session_id, stored: r.stored_session_id ?? row.id, readOnly: false });
        let next: ChatState = { items: fromTranscript(r.messages ?? []), busy: !!r.running, title: row.title };
        for (const req of r.open_requests ?? []) {
          if (ANSWERED_REQUESTS.has(req.method)) next = applyRequest(next, req.id, req.method, req.params);
        }
        setChat(next);
      } catch (e) {
        setCurrent({ sid: null, stored: null, readOnly: false });
        notice((e as Error).message, "error");
      }
    },
    [gw, fetcher, notice],
  );

  const answer = useCallback(
    (requestId: string, result: Record<string, unknown>, label: string) => {
      if (!gw) return;
      gw.respond(requestId, result);
      setChat((s) => markAnswered(s, requestId, label));
    },
    [gw],
  );

  /** What the composer sends: a slash command or a prompt. Returns text to put back in the composer, if any. */
  const send = useCallback(
    async function run(raw: string): Promise<{ prefill?: string; openSessions?: boolean } | void> {
      const text = raw.trim();
      if (!text || !gw) return;
      const cmd = splitCommand(text);
      if (!cmd) return submit(text);
      const [name, rest] = cmd;
      const { local, reach } = classify(name, catalog);
      if (local) {
        const canonical = catalog?.canon.get(name) ?? name;
        if (["/new", "/reset", "/clear"].includes(canonical) || ["/new", "/reset", "/clear"].includes(name)) {
          newSession();
          return;
        }
        if (name === "/resume" || name === "/sessions") {
          await refreshSessions();
          return { openSessions: true };
        }
        if (name === "/stop") return interrupt();
        if (name === "/retry") {
          if (lastUserText.current) return submit(lastUserText.current);
          notice("Nothing to retry yet.");
          return;
        }
      }
      if (reach !== "ok") {
        notice(reachNote(name, reach));
        return;
      }
      let sid: string;
      try {
        sid = await ensureSession();
      } catch (e) {
        notice((e as Error).message, "error");
        return;
      }
      push({ kind: "notice", id: nextId("n"), text: text, tone: "command" });
      try {
        const r = await gw.call<{
          output?: string | null;
          warning?: string | null;
          type?: string | null;
          message?: string | null;
          display?: string | null;
          target?: string | null;
          notice?: string | null;
        }>("slash.exec", { session_id: sid, command: `${name}${rest ? ` ${rest}` : ""}` }, 120_000);
        if (r.type === "skill" || r.type === "send") {
          if (r.message) await submit(r.message, r.display || text);
          return;
        }
        if (r.type === "prefill") return { prefill: r.message ?? "" };
        if (r.type === "alias" && r.target) return run(r.target);
        const out = [r.output, r.notice, r.warning].filter(Boolean).join("\n").trim();
        notice(out || `${name}: done`, "info");
      } catch (e) {
        notice(`${name}: ${(e as Error).message}`, "error");
      }
    },
    [gw, catalog, submit, newSession, refreshSessions, interrupt, ensureSession, notice, push],
  );

  const complete = useCallback(
    async (text: string): Promise<CompletionRow[]> => {
      if (!gw || gw.state !== "open") return [];
      try {
        const r = await gw.call("complete.slash", { text, ...(sidRef.current ? { session_id: sidRef.current } : {}) }, 10_000);
        return completionRows(r, catalog);
      } catch {
        return [];
      }
    },
    [gw, catalog],
  );

  return useMemo(
    () => ({
      conn,
      chat,
      current,
      sessions,
      sessionsError,
      model,
      error,
      profile: gw?.profile ?? "",
      connect,
      send,
      complete,
      newSession,
      interrupt,
      openSession,
      refreshSessions,
      answer,
    }),
    [conn, chat, current, sessions, sessionsError, model, error, gw, connect, send, complete, newSession, interrupt, openSession, refreshSessions, answer],
  );
}

export type ChatApi = ReturnType<typeof useChat>;
