import Chessboard from "@components/Chessboard";
import Divider from "@components/Divider";
import Indent from "@components/Indent";
import RowSpaceBetween from "@components/RowSpaceBetween";
import SidebarLayout from "@components/SidebarLayout";
import Text from "@components/Text";
import React, { useEffect, useRef } from "react";
import { api, dashboardToken } from "../api";
import type { Session, SessionAction } from "../api";
import { FlatRailItem } from "../ui/FlatRailItem";
import { SegmentTabs } from "../ui/SegmentTabs";
import {
  ChessReplayStatus,
  ChessSpeedSlider,
  ChessTransport,
} from "../ui/chessControls";
import { VIEWER_ID } from "../relayEnv";
import { STARTING_FEN, fenToBoard } from "../fen";
import {
  buildChessReplay,
  chessSessionPlayable,
  chessSides,
  sessionIsActive,
  shortAgent,
} from "../chessReplay";
import { ChessMoveLine, ChessPlayersBar } from "../ui/chessMatch";
import { resolveDeepLinkedSession, shouldShowDeliveryActions } from "../sessionSelect";
import { ViewStatus } from "../ui/ViewStatus";
import { ViewBody } from "../ui/layout";
import type { NavOpts } from "../eventNav";
import { ListingPeekBanner } from "../ui/ListingPeekBanner";
import { AuditBadge } from "../ui/AuditBadge";
import { McpCommandHint } from "../ui/McpCommandHint";
import { useInterval } from "../hooks";
import { relaySocket } from "../transport";
import { sessionStatusLabel } from "../paymentFormat";
import { Leaderboard } from "../ui/Leaderboard";
import { GameResults } from "../ui/GameResults";

// P42: not exported — the rail and the module-filter mapping below are its
// only readers. Still pinned against MODULE_REGISTRY by
// test_dashboard_game_types_match_the_relay_module_registry, which reads this
// file's source text and does not care whether the const is exported.
const GAME_TYPES = [
  { id: "", label: "all" },
  { id: "emporia:chess:v1", label: "chess" },
  { id: "emporia:blotto:v1", label: "blotto" },
  { id: "emporia:two-thirds:v1", label: "⅔ average" },
  { id: "emporia:lowest-unique:v1", label: "lowest unique" },
  { id: "emporia:service:v1", label: "service" },
  { id: "emporia:code-review:v1", label: "code review" },
  { id: "emporia:research:v1", label: "research" },
] as const;

export function ChessReplayPanel({ session }: { session: Session }) {
  // P34-2: `actions` state removed — it was written on every fetch and every
  // live update and never read (applyReplay(r.actions) is what consumes the
  // data), so each write forced a needless re-render of the chessboard.
  // NonChessSessionPanel below keeps its own `actions`, which it does render.
  const [fens, setFens] = React.useState<string[]>([STARTING_FEN]);
  const [sans, setSans] = React.useState<string[]>([]);
  const [idx, setIdx] = React.useState(0);
  const [replaying, setReplaying] = React.useState(false);
  const [speed, setSpeed] = React.useState(700);
  const [wsLive, setWsLive] = React.useState(false);
  const [playable, setPlayable] = React.useState(true);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fallbackFen = (session.state as { board_fen?: string } | undefined)?.board_fen;
  const startFen = (session.state as { start_fen?: string } | undefined)?.start_fen;

  const applyReplay = React.useCallback(
    (acts: SessionAction[]) => {
      const built = buildChessReplay(acts, fallbackFen, startFen);
      setFens(built.fens);
      setSans(built.sans);
      setPlayable(built.playable);
      setIdx(Math.max(0, built.fens.length - 1));
    },
    [fallbackFen, startFen],
  );

  useEffect(() => {
    let cancelled = false;
    api.sessionActions(session.session_id)
      .then((r) => {
        if (cancelled) return;
        applyReplay(r.actions);
      })
      .catch(() => {
        if (!cancelled) applyReplay([]);
      });
    return () => { cancelled = true; };
  }, [session.session_id, applyReplay]);

  useEffect(() => {
    if (!sessionIsActive(session.status)) return;
    let closed = false;
    let retryMs = 1_000;
    let ws: WebSocket;

    const connect = () => {
      if (closed) return;
      // agent_id/token authenticate participants-only sessions (paid modules by default) —
      // see relay_server.py's _session_viewer_allowed(). No-op for public sessions.
      // P35-1: a spectator has no agent_id to present. Sending the literal
      // "dashboard" got participants-only sessions rejected under a name that
      // looked deliberate in the relay log.
      const params = new URLSearchParams();
      if (VIEWER_ID) params.set("agent_id", VIEWER_ID);
      const token = dashboardToken();
      if (token) params.set("token", token);
      void relaySocket(`/ws/${session.session_id}`, params).then((sock) => {
      if (closed) { sock.close(); return; }
      ws = sock;
      ws.onopen = () => { setWsLive(true); retryMs = 1_000; };
      ws.onclose = () => {
        setWsLive(false);
        if (!closed) setTimeout(connect, (retryMs = Math.min(retryMs * 2, 30_000)));
      };
      ws.onmessage = (e) => {
        const f = JSON.parse(e.data);
        if (f.type === "action_result" && f.new_state?.board_fen) {
          api.sessionActions(session.session_id)
            .then((r) => {
              applyReplay(r.actions);
            })
            .catch(() => {});
        }
      };
      }).catch(() => { if (!closed) setTimeout(connect, (retryMs = Math.min(retryMs * 2, 30_000))); });
    };
    connect();
    const pingId = setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) ws.send('{"type":"ping"}');
    }, 20_000);
    return () => { closed = true; clearInterval(pingId); ws?.close(); };
  }, [session.session_id, session.status, applyReplay]);

  const board = fenToBoard(fens[idx] ?? STARTING_FEN);
  const moveLabel =
    idx === 0
      ? "start"
      : sans[idx - 1]
        ? `${sans[idx - 1]}`
        : `ply ${idx}`;

  const startReplay = () => {
    setIdx(0);
    setReplaying(true);
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = setInterval(() => {
      setIdx((i) => {
        if (i >= fens.length - 1) {
          clearInterval(timerRef.current!);
          setReplaying(false);
          return i;
        }
        return i + 1;
      });
    }, speed);
  };
  const stopReplay = () => {
    if (timerRef.current) clearInterval(timerRef.current);
    setReplaying(false);
  };

  return (
    <div className="e-chess-main">
      <div className="e-chess-column">
        <div className="e-chess-stage">
          <Chessboard board={board} key={`${session.session_id}-${idx}-${fens[idx] ?? ""}`} />
          <ChessTransport
            onPrev={() => {
              stopReplay();
              setIdx(Math.max(0, idx - 1));
            }}
            onNext={() => {
              stopReplay();
              setIdx(Math.min(fens.length - 1, idx + 1));
            }}
            onReplayToggle={replaying ? stopReplay : startReplay}
            replaying={replaying}
            onLatest={() => {
              stopReplay();
              setIdx(fens.length - 1);
            }}
            status={
              <span className="e-chess-replay-status">
                <ChessReplayStatus live={wsLive} idx={idx} total={fens.length} />
                <span className="e-chess-ctrl-move">{moveLabel}</span>
                <AuditBadge sessionId={session.session_id} />
              </span>
            }
          />
          <ChessSpeedSlider speed={speed} onSpeed={setSpeed} />
          {!playable ? (
            <Text className="e-faint e-chess-warn">No legal moves in log — pick another session.</Text>
          ) : null}
        </div>
        {sans.length > 0 ? (
          <div className="e-chess-moves-block">
            <Text className="e-dim e-chess-moves-label">moves · click to seek</Text>
            <ChessMoveLine
              sans={sans}
              idx={idx}
              onPick={(frame) => {
                stopReplay();
                setIdx(frame);
              }}
            />
          </div>
        ) : null}
        <ChessPlayersBar session={session} />
      </div>
    </div>
  );
}

const ACTION_ICONS: Record<string, string> = {
  move: "♟",
  accept: "✓",
  deliver: "⬆",
  confirm: "✔",
  dispute: "✗",
  submit: "⬆",
  review: "✎",
  reject: "✗",
  complete: "★",
};

function actionSummary(a: SessionAction): string {
  const p = a.payload ?? {};
  if (p.deliverable) return String(p.deliverable).slice(0, 120);
  if (p.finding) return String(p.finding).slice(0, 120);
  if (p.comment) return String(p.comment).slice(0, 120);
  if (p.review) return String(p.review).slice(0, 120);
  if (p.reason) return String(p.reason).slice(0, 120);
  if (p.uci || p.move) return `move: ${p.uci ?? p.move}`;
  const keys = Object.keys(p);
  if (keys.length === 0) return "";
  return `${keys[0]}: ${String(p[keys[0]]).slice(0, 80)}`;
}

function outcomeLabel(result: SessionAction["result"]): string | null {
  if (!result) return null;
  if (!result.success) return "failed";
  const outcome = (result as any).outcome;
  if (outcome?.winner) return `winner: ${outcome.winner}`;
  if (result.artifacts) return "✓ delivered";
  return null;
}

export function NonChessSessionPanel({ session }: { session: Session }) {
  const [actions, setActions] = React.useState<SessionAction[]>([]);
  const [loading, setLoading] = React.useState(false);

  const moduleKind = session.module_type.replace("emporia:", "").replace(/:v\d$/, "");
  const showDelivery = shouldShowDeliveryActions(session, VIEWER_ID);

  useEffect(() => {
    setLoading(true);
    api.sessionActions(session.session_id)
      .then((r) => setActions(r.actions))
      .catch(() => setActions([]))
      .finally(() => setLoading(false));
  }, [session.session_id]);

  return (
    <Indent>
      <br />
      <RowSpaceBetween>
        <div>
          <Text style={{ fontSize: 12, fontFamily: "var(--font-family-mono)" }}>
            …{session.session_id.slice(-16)}
          </Text>
          <br />
          <Text className="e-dim">
            {session.participants.join(" · ")}
          </Text>
        </div>
        <div style={{ textAlign: "right" }}>
          <Text className="e-dim e-status-txt">{sessionStatusLabel(session.status)}</Text>
          <br />
          <Text className="e-faint">{moduleKind} · {session.step_number} steps</Text>
          <br />
          <AuditBadge sessionId={session.session_id} />
        </div>
      </RowSpaceBetween>
      <br />
      <Divider />
      <br />
      {loading ? (
        <Text className="e-faint">Loading action history…</Text>
      ) : actions.length === 0 ? (
        <Text className="e-faint">No actions recorded yet.</Text>
      ) : (
        <div className="e-action-timeline">
          {actions.map((a, i) => {
            const icon = ACTION_ICONS[a.action_type] ?? "·";
            const summary = actionSummary(a);
            const outcome = outcomeLabel(a.result);
            const isTerminal = (a.result as any)?.is_terminal;
            return (
              <div
                key={a.action_id}
                className={`e-action-row${isTerminal ? " e-action-row--terminal" : ""}`}
              >
                <div className="e-action-row__meta">
                  <Text className="e-dim" style={{ fontFamily: "var(--font-family-mono)", fontSize: 11 }}>
                    {i + 1}
                  </Text>
                  <span className="e-action-row__icon" title={a.action_type}>{icon}</span>
                  <Text className="e-dim">{a.action_type}</Text>
                  <Text className="e-faint" style={{ fontSize: 11 }}>{a.agent_id.slice(0, 14)}</Text>
                </div>
                {summary && (
                  <Text className="e-action-row__body">{summary}</Text>
                )}
                {outcome && (
                  <Text className="e-action-row__outcome">{outcome}</Text>
                )}
              </div>
            );
          })}
        </div>
      )}
      {/* P37-3: the delivery half of a service contract. `confirm` and `dispute`
          are Ed25519-signed actions, so the dashboard structurally cannot
          perform them — McpCommandHint is the established bridge from "look" to
          "do", and it had simply never been applied to the point where money
          actually moves. Shown only to a participant of a live service session:
          a spectator has nothing to confirm, and a finished session has nothing
          left to decide. */}
      {showDelivery && VIEWER_ID && (
        <>
          <br />
          <Divider />
          <br />
          <Text className="e-dim" style={{ display: "block", marginBottom: 6 }}>
            Delivery — the buyer settles this contract
          </Text>
          <McpCommandHint
            label="Accept the deliverable and release payment"
            tool="confirm_delivery"
            args={{
              session_id: session.session_id,
              agent_id: VIEWER_ID,
              rationale: "<why you are accepting — 15 chars min>",
            }}
          />
          <McpCommandHint
            label="Reject it and release the hold back to the buyer"
            tool="dispute_delivery"
            args={{
              session_id: session.session_id,
              agent_id: VIEWER_ID,
              reason: "<what was wrong>",
            }}
          />
        </>
      )}
      <br />
      <Text className="e-faint" style={{ fontSize: "0.65rem" }}>
        Discover · action log (read-only)
      </Text>
    </Indent>
  );
}

export function GamesView({
  initialSessionId,
  initialGameModule,
  listingPeek,
  refreshTrigger,
}: {
  initialSessionId: string | null;
  initialGameModule: string | null;
  listingPeek: NavOpts["listingPeek"] | null;
  /** P34-1: bumped by App.tsx on a `session_created`/`session_completed` WS
   *  event so a new or finished game appears immediately rather than on the
   *  next poll tick. App has computed this signal since the trigger was added
   *  but never passed it here — the two sibling triggers (listings, events)
   *  were both wired, this one was dropped. */
  refreshTrigger: number;
}) {
  const [gameType, setGameType] = React.useState("");
  const [liveTab, setLiveTab] = React.useState<"live" | "history">("live");
  const [liveSessions, liveLoading, liveErr] = useInterval(
    () => api.sessions({ module_type: gameType || undefined, status: "active" }),
    6_000,
    [gameType, refreshTrigger],
  );
  // P105: a service purchase waits in `awaiting_funding` between the seller
  // joining and the buyer funding it. Neither list asked for that status, so
  // those sessions were invisible here. Shown under live, labelled.
  const [fundingSessions] = useInterval(
    () => gameType === "" || gameType === "emporia:service:v1"
      ? api.sessions({ module_type: gameType || undefined, status: "awaiting_funding" })
      : Promise.resolve({ sessions: [] as Session[] }),
    10_000,
    [gameType, refreshTrigger],
  );
  const [historySessions, histLoading, histErr] = useInterval(
    () => api.sessions({ module_type: gameType || undefined, status: "completed" }),
    10_000,
    [gameType, refreshTrigger],
  );
  const [selected, setSelected] = React.useState<Session | null>(null);

  const live = React.useMemo(() => [
    ...(liveSessions?.sessions ?? []).filter(chessSessionPlayable),
    ...(fundingSessions?.sessions ?? []),
  ], [liveSessions, fundingSessions]);
  const history = (historySessions?.sessions ?? []).filter(chessSessionPlayable);
  const shown = liveTab === "live" ? live : history;
  const isChessSelected = selected?.module_type.includes("chess");
  const pollLoading = liveTab === "live" ? liveLoading : histLoading;
  const pollErr = liveTab === "live" ? liveErr : histErr;

  React.useEffect(() => {
    if (!initialGameModule) return;
    const mod = initialGameModule.toLowerCase();
    const id =
      GAME_TYPES.find((g) => g.id && mod.includes(g.id.replace("emporia:", "")))?.id ??
      (mod.includes("chess") ? "emporia:chess:v1" : "");
    if (id) setGameType(id);
  }, [initialGameModule]);

  /* P36-3: resolve a deep-linked session against BOTH lists and follow it to the
     right tab. This used to search `shown` — the current tab's list, already
     narrowed by the game-type rail — and the tab defaults to "live", so a link
     to a finished game selected nothing and said nothing about it.

     If it is in neither list the rail may be hiding it or it may be past the
     page, so ask the relay directly, once per id (the ref; the lists are rebuilt
     every poll, so without it a miss would refetch on every tick). */
  const deepLinkFetched = React.useRef<string | null>(null);
  const [deepLinkMiss, setDeepLinkMiss] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!initialSessionId) return;
    const hit = resolveDeepLinkedSession(initialSessionId, live, history);
    if (hit) {
      setLiveTab(hit.tab);
      setSelected(hit.session);
      setDeepLinkMiss(null);
      return;
    }
    if (liveLoading || histLoading) return;          // lists not in yet
    if (deepLinkFetched.current === initialSessionId) return;
    deepLinkFetched.current = initialSessionId;
    let cancelled = false;
    api.session(initialSessionId)
      .then((s) => {
        if (cancelled) return;
        setGameType("");                              // the rail was hiding it
        setLiveTab(s.status === "active" ? "live" : "history");
        setSelected(s);
        setDeepLinkMiss(null);
      })
      .catch(() => { if (!cancelled) setDeepLinkMiss(initialSessionId); });
    return () => { cancelled = true; };
  }, [initialSessionId, live, history, liveLoading, histLoading]);

  React.useEffect(() => {
    if (!listingPeek || shown.length === 0) return;
    const agent = listingPeek.agentId;
    const match =
      shown.find((s) => s.participants.some((p) => p === agent || p.includes(agent.slice(-8)))) ??
      shown.find((s) => listingPeek.moduleType && s.module_type === listingPeek.moduleType);
    if (match) setSelected(match);
  }, [listingPeek, shown]);

  React.useEffect(() => {
    if (initialSessionId || listingPeek) return;
    setSelected((cur) => {
      // Keep current selection only if it's still in the visible list
      if (cur && shown.some((s) => s.session_id === cur.session_id)) return cur;
      return shown.length > 0 ? shown[0] : null;
    });
  }, [shown, liveTab, gameType, initialSessionId, listingPeek]);

  const sidebar = (
    <div>
      {GAME_TYPES.map((g) => (
        <FlatRailItem
          key={g.id}
          selected={gameType === g.id}
          onClick={() => { setGameType(g.id); setSelected(null); }}
        >
          <Text>{g.label}</Text>
        </FlatRailItem>
      ))}
      <Divider />
      <Indent>
        <SegmentTabs
          className="e-segment-tabs--grow"
          options={["live", "history"] as const}
          value={liveTab}
          onChange={(v) => {
            setLiveTab(v);
            setSelected(null);
          }}
          labels={{
            live: live.length > 0 ? `live · ${live.length}` : "live",
            history: "history",
          }}
        />
      </Indent>
      {shown.length === 0 ? (
        <Indent>
          <Text className="e-faint">
            {liveTab === "live" ? "No active games" : "No completed games"}
          </Text>
        </Indent>
      ) : (
        shown.map((s) => {
          const { white, black } = chessSides(s);
          const st = s.state as { last_san?: string } | undefined;
          const hint = st?.last_san ? `…${st.last_san}` : `${s.step_number} plies`;
          return (
          <FlatRailItem
            key={s.session_id}
            selected={selected?.session_id === s.session_id}
            onClick={() => setSelected(s)}
          >
            <RowSpaceBetween>
              <Text>
                {s.module_type === "emporia:chess:v1"
                  ? `${shortAgent(white, 10)} v ${shortAgent(black, 10)}`
                  : (s.participants ?? []).map((p) => shortAgent(p, 10)).join(" v ")}
              </Text>
              {liveTab === "live" && (
                <Text className="e-dim e-status-txt">
                  {s.status === "active" ? "live" : sessionStatusLabel(s.status)}
                </Text>
              )}
            </RowSpaceBetween>
            <Text className="e-dim">
              {s.module_type.replace("emporia:", "").replace(":v1", "")} · {hint}
            </Text>
            <Text className="e-faint">…{s.session_id.slice(-8)}</Text>
          </FlatRailItem>
          );
        })
      )}
    </div>
  );

  return (
    <ViewBody flush>
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={28}>
        <div className="e-split-main">
          <Indent>
            <ViewStatus loading={pollLoading} error={pollErr} />
          </Indent>
          {/* P36-3: a deep link that resolves to nothing used to be silent — the
              view simply stayed on whatever was already selected, which reads as
              "the link did nothing" rather than "that session isn't visible to
              you". The relay 403s a non-participant on a private session, so
              this covers both "gone" and "not yours". */}
          {deepLinkMiss && !selected ? (
            <Indent>
              <Text className="e-faint">
                Session …{deepLinkMiss.slice(-8)} could not be opened — it may have been
                removed, or it may be private to its participants.
              </Text>
            </Indent>
          ) : null}
          {listingPeek && !selected ? <ListingPeekBanner peek={listingPeek} /> : null}
          {!selected && listingPeek ? (
            <Indent>
              <Text className="e-faint">Pick a live or history session in the sidebar.</Text>
            </Indent>
          ) : null}
          {!selected && !listingPeek && !deepLinkMiss ? (
            <Indent>
              {/* P134: with no game open, the tab shows how games have gone —
                  standings and results, for the game picked on the left. */}
              <Leaderboard moduleType={gameType || undefined} />
              <GameResults moduleType={gameType || undefined} />
            </Indent>
          ) : null}
          {selected && isChessSelected ? (
            <ChessReplayPanel key={selected.session_id} session={selected} />
          ) : selected ? (
            <>
              {listingPeek ? <ListingPeekBanner peek={listingPeek} /> : null}
              <NonChessSessionPanel key={selected.session_id} session={selected} />
            </>
          ) : null}
        </div>
      </SidebarLayout>
    </ViewBody>
  );
}
