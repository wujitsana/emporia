import Chessboard from "@kit/Chessboard";
import Divider from "@kit/Divider";
import Indent from "@kit/Indent";
import RowSpaceBetween from "@kit/RowSpaceBetween";
import SidebarLayout from "@kit/SidebarLayout";
import Text from "@kit/Text";
import React, { useEffect, useRef } from "react";
import { OPENED_TAG, SEALED_TAG, api, dashboardToken, looksLikeEncryptedEnvelope } from "../api";
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
import { isJobModule, type NavOpts } from "../eventNav";
import { ListingPeekBanner } from "../ui/ListingPeekBanner";
import { AuditBadge } from "../ui/AuditBadge";
import { McpCommandHint } from "../ui/McpCommandHint";
import { useInterval } from "../hooks";
import { OpenChallenges } from "./QuestArcs";
import { relaySocket } from "../transport";
import { sessionStatusLabel } from "../paymentFormat";
import { whenText } from "../relayFormat";
import { Leaderboard } from "../ui/Leaderboard";
import { GameResults } from "../ui/GameResults";
import { GameChat } from "../ui/GameChat";
import { VerifyBadge } from "../ui/VerifyBadge";
import { SessionMoney } from "../ui/SessionMoney";
import { PurchaseConversation } from "../ui/PurchaseConversation";
import { openSealed, sealedItems } from "../sealedSession";
import { SET_PLAYS, gameBase, isBargain, isSetPlay, isTablePlay, phaseLine, setPlayView } from "../setPlay";
import { sessionMatches, type SessionSection } from "../sessionSelect";
import { SetPlayPanel } from "../ui/SetPlayPanel";
import { BargainPanel } from "../ui/BargainPanel";
import { Ladder } from "./GamifyView";
import { AskAgent } from "../chat/AskAgent";
import { gamePrompt } from "../chat/askPrompts";
import { PixelBadge } from "../badges";

// P42: not exported — the rail and the module-filter mapping below are its
// only readers. Still pinned against MODULE_REGISTRY by
// test_dashboard_game_types_match_the_relay_module_registry, which reads this
// file's source text and does not care whether the const is exported.
// Set plays first; chess last among the games and never the default; jobs
// belong to Deals (docs/ui-ux-structure.md).
const GAME_TYPES = [
  // Calls: the set plays are `:v2` (the `:v1` ones are retired; a chip matches either by name).
  { id: "emporia:blotto:v2", label: "blotto", kind: "set" },
  { id: "emporia:two-thirds:v2", label: "⅔ average", kind: "set" },
  { id: "emporia:lowest-unique:v2", label: "lowest unique", kind: "set" },
  { id: "emporia:bargain:v2", label: "bargain", kind: "set" },
  { id: "emporia:chess:v1", label: "chess", kind: "chess" },
  { id: "emporia:service:v1", label: "service", kind: "job" },
  { id: "emporia:code-review:v1", label: "code review", kind: "job" },
  { id: "emporia:research:v1", label: "research", kind: "job" },
] as const;

/** The filter chips a section offers; "" is the section's default (set plays / all deals). */
function filtersFor(section: SessionSection): { id: string; label: string }[] {
  return section === "games"
    ? [{ id: "", label: "set plays" }, ...GAME_TYPES.filter((g) => g.kind !== "job")]
    : [{ id: "", label: "all deals" }, ...GAME_TYPES.filter((g) => g.kind === "job")];
}

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

  // P236: without the socket (a Hermes profile other than the launch one),
  // follow a live game by asking every few seconds.
  useEffect(() => {
    if (!sessionIsActive(session.status) || wsLive) return;
    const id = setInterval(() => {
      api.sessionActions(session.session_id).then((r) => applyReplay(r.actions)).catch(() => {});
    }, 5_000);
    return () => clearInterval(id);
  }, [session.session_id, session.status, wsLive, applyReplay]);

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
                {session.created_at && <span className="e-faint">started {whenText(session.created_at)}</span>}
                <AuditBadge sessionId={session.session_id} />
                <VerifyBadge sessionId={session.session_id} live={session.status === "active"} />
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
        <SessionMoney session={session} />
        <GameChat sessionId={session.session_id} live={session.status === "active" || session.status === "waiting"} />
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
  commit: "■",
  reveal: "◆",
  review: "✎",
  reject: "✗",
  complete: "★",
};

function actionSummary(a: SessionAction, opened: Record<string, string> = {}): string {
  // P149: a private session's sealed text — opened on this machine, or 🔒.
  const p: Record<string, unknown> = { ...(a.payload ?? {}) };
  for (const [k, v] of Object.entries(p)) {
    if (typeof v === "string" && looksLikeEncryptedEnvelope(v)) {
      p[k] = opened[`${a.action_id}:${k}`] !== undefined ? `${OPENED_TAG} ${opened[`${a.action_id}:${k}`]}` : `${SEALED_TAG} sealed to the players`;
    }
  }
  if (p.deliverable) return String(p.deliverable).slice(0, 120);
  if (p.finding) return String(p.finding).slice(0, 120);
  if (p.comment) return String(p.comment).slice(0, 120);
  if (p.review) return String(p.review).slice(0, 120);
  if (p.reason) return String(p.reason).slice(0, 120);
  // P143: commit-reveal moves read as what they are.
  if (a.action_type === "commit") return "committed a hidden move";
  if (a.action_type === "reveal") return `revealed: ${JSON.stringify(p.move)}`;
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
  // A game's verdict rides in artifacts.result; only a service has a delivery.
  const verdict = (result.artifacts as { result?: { winner?: string | null; outcome_type?: string } } | undefined)?.result;
  if (verdict) return verdict.winner ? `winner: ${verdict.winner}` : verdict.outcome_type ?? "ended";
  if (result.artifacts) return "✓ delivered";
  return null;
}

export function NonChessSessionPanel({ session }: { session: Session }) {
  const [actions, setActions] = React.useState<SessionAction[]>([]);
  const [loading, setLoading] = React.useState(true);
  // A different session starts loading again (compared while rendering).
  const [loadedSessionId, setLoadedSessionId] = React.useState(session.session_id);
  if (loadedSessionId !== session.session_id) {
    setLoadedSessionId(session.session_id);
    setLoading(true);
  }

  const moduleKind = session.module_type.replace("emporia:", "").replace(/:v\d$/, "");
  const showDelivery = shouldShowDeliveryActions(session, VIEWER_ID);

  const [opened, setOpened] = React.useState<Record<string, string>>({});
  useEffect(() => {
    api.sessionActions(session.session_id)
      .then((r) => {
        setActions(r.actions);
        const items = sealedItems(r.actions.map((a) => ({ id: a.action_id, payload: a.payload })));
        openSealed(session.session_id, items).then(setOpened).catch(() => setOpened({}));
      })
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
          {session.created_at && (<><br /><Text className="e-faint">started {whenText(session.created_at)}</Text></>)}
          <br />
          <AuditBadge sessionId={session.session_id} />
          <br />
          <VerifyBadge sessionId={session.session_id} live={session.status === "active"} />
          <br />
          <SessionMoney session={session} />
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
            const summary = actionSummary(a, opened);
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
      <GameChat sessionId={session.session_id} live={session.status === "active" || session.status === "waiting"} />
      {session.module_type === "emporia:service:v1" ? (
        <PurchaseConversation session={session} viewer={VIEWER_ID} />
      ) : null}
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

/**
 * True on the first render and on any render where `deps` differ from the
 * previous render's. Lets a view apply "when X changes, adjust my state"
 * during render (React's recommended alternative to an effect that only calls
 * setState), without a ref.
 */
function useChanged(deps: readonly unknown[]): boolean {
  const [prev, setPrev] = React.useState<readonly unknown[] | null>(null);
  const changed = prev === null || prev.length !== deps.length || deps.some((d, i) => !Object.is(d, prev[i]));
  if (changed) setPrev(deps);
  return changed;
}

/** One game's standing: its Craft ladder, its standings and its recent results. */
function LaddersPanel({ moduleType, onOpen }: { moduleType: string; onOpen: (sessionId: string) => void }) {
  const label = GAME_TYPES.find((g) => gameBase(g.id) === gameBase(moduleType))?.label ?? moduleType;
  return (
    <div className="e-split-main">
      <Text className="e-subhead">{label} — Craft ladder, this relay</Text>
      <Ladder moduleType={moduleType} />
      <Text className="e-subhead">{label} — standings</Text>
      <Leaderboard moduleType={moduleType} />
      <Text className="e-subhead">{label} — recent results</Text>
      <GameResults moduleType={moduleType} onOpen={onOpen} />
    </div>
  );
}

/** A rail row: what is being played, by whom, and where the table is. */
function SessionRow({ s, showStatus }: { s: Session; showStatus: boolean }) {
  const kind = s.module_type.replace("emporia:", "").replace(/:v\d+$/, "");
  const label = GAME_TYPES.find((g) => gameBase(g.id) === gameBase(s.module_type))?.label ?? kind;
  if (isSetPlay(s.module_type)) {
    const v = setPlayView(s);
    const n = s.participants?.length ?? 0;
    return (
      <span className="e-game-row">
        <span className="e-game-row__top">
          <span className="e-game-row__kind"><PixelBadge id={s.module_type} size={1} /> {label}</span>
          {s.status === "active" && <PixelBadge id="live" size={1} />}
          <span className="e-game-row__phase">{phaseLine(v)}</span>
        </span>
        <Text>{n <= 2 ? s.participants.map((p) => shortAgent(p, 10)).join(" v ") : `${n} players · ${s.participants.slice(0, 3).map((p) => shortAgent(p, 8)).join(", ")}${n > 3 ? "…" : ""}`}</Text>
        <Text className="e-faint">…{s.session_id.slice(-8)}{s.created_at ? ` · ${whenText(s.created_at)}` : ""}</Text>
      </span>
    );
  }
  const { white, black } = chessSides(s);
  const st = s.state as { last_san?: string } | undefined;
  const hint = st?.last_san ? `…${st.last_san}` : `${s.step_number} ${s.module_type === "emporia:chess:v1" ? "plies" : "steps"}`;
  return (
    <span className="e-game-row">
      <span className="e-game-row__top">
        <span className="e-game-row__kind">{!isJobModule(s.module_type) && <><PixelBadge id={s.module_type} size={1} /> </>}{label}</span>
        {showStatus && <span className="e-game-row__phase">{s.status === "active" ? "live" : sessionStatusLabel(s.status)}</span>}
      </span>
      <Text>
        {s.module_type === "emporia:chess:v1"
          ? `${shortAgent(white, 10)} v ${shortAgent(black, 10)}`
          : (s.participants ?? []).map((p) => shortAgent(p, 10)).join(" & ")}
      </Text>
      <Text className="e-faint">{hint} · …{s.session_id.slice(-8)}{s.created_at ? ` · ${whenText(s.created_at)}` : ""}</Text>
    </span>
  );
}

type RailTab = "live" | "history" | "ladders";
/** One empty list for the ladders tab: a fresh `[]` each render looks "changed" to useChanged and loops. */
const NO_SESSIONS: Session[] = [];

/**
 * Games (set plays first, then chess) or Deals (service, code review,
 * research): the same list-then-detail shape over the relay's sessions, split
 * by what a viewer does with them — a game is played and won, a deal is
 * delivered and paid.
 */
export function GamesView({
  section = "games",
  initialSessionId,
  initialGameModule,
  listingPeek,
  refreshTrigger,
  onSessionShown,
}: {
  section?: SessionSection;
  initialSessionId: string | null;
  initialGameModule: string | null;
  listingPeek: NavOpts["listingPeek"] | null;
  /** P34-1: bumped by App.tsx on a session event, so a new or finished game
   *  appears immediately rather than on the next poll tick. */
  refreshTrigger: number;
  /** Told which session is open (or none), so the route can name it. */
  onSessionShown?: (sessionId: string | null) => void;
}) {
  const games = section === "games";
  const filters = filtersFor(section);
  const [gameType, setGameType] = React.useState("");
  const [liveTab, setLiveTab] = React.useState<RailTab>("live");
  const [ladderGame, setLadderGame] = React.useState<string>(SET_PLAYS[0]);
  const query = gameType || undefined;
  const [liveSessions, liveLoading, liveErr] = useInterval(
    () => api.sessions({ module_type: query, status: "active" }),
    6_000,
    [gameType, refreshTrigger],
  );
  // Games: a table waiting for players is live too. Deals: P105 — a purchase
  // waits in `awaiting_funding` between the seller joining and the buyer funding.
  const [pendingSessions] = useInterval(
    () => api.sessions({ module_type: query, status: games ? "waiting" : "awaiting_funding" }),
    10_000,
    [gameType, refreshTrigger, games],
  );
  const [historySessions, histLoading, histErr] = useInterval(
    () => api.sessions({ module_type: query, status: "completed" }),
    10_000,
    [gameType, refreshTrigger],
  );
  const [selected, setSelected] = React.useState<Session | null>(null);

  const mine = React.useCallback((s: Session) => sessionMatches(s, section, gameType), [section, gameType]);
  const live = React.useMemo(() => [
    ...(liveSessions?.sessions ?? []).filter(chessSessionPlayable),
    ...(pendingSessions?.sessions ?? []),
  ].filter(mine), [liveSessions, pendingSessions, mine]);
  const history = React.useMemo(
    () => (historySessions?.sessions ?? []).filter(chessSessionPlayable).filter(mine),
    [historySessions, mine],
  );
  const ladders = games && liveTab === "ladders";
  const shown = liveTab === "live" ? live : liveTab === "history" ? history : NO_SESSIONS;
  const pollLoading = liveTab === "live" ? liveLoading : histLoading;
  const pollErr = liveTab === "live" ? liveErr : histErr;

  // The selection rules below run while rendering, when their inputs change
  // (see useChanged) — they only set this view's own state from its props and
  // polled lists, so they are derived here rather than synced from effects.
  if (useChanged([initialGameModule]) && initialGameModule) {
    const mod = initialGameModule.toLowerCase();
    const hit = GAME_TYPES.find((g) => mod.includes(gameBase(g.id).replace("emporia:", "")));
    // A set play opens on the set plays (so its table is in the list); chess and jobs on their own chip.
    if (hit) setGameType(hit.kind === "set" ? "" : hit.id);
  }

  /* P36-3: resolve a deep-linked session against BOTH lists and follow it to the
     right tab. If it is in neither list the filter may be hiding it or it may be
     past the page, so ask the relay directly, once per id (the ref; the lists are
     rebuilt every poll, so without it a miss would refetch on every tick). */
  const deepLinkFetched = React.useRef<string | null>(null);
  const [deepLinkMiss, setDeepLinkMiss] = React.useState<string | null>(null);

  // A deep link is followed once: after that, a click on another session sticks.
  const [deepLinkDone, setDeepLinkDone] = React.useState<string | null>(null);
  const deepLinkInputsChanged = useChanged([initialSessionId, live, history]);
  if (deepLinkInputsChanged && initialSessionId && deepLinkDone !== initialSessionId) {
    const hit = resolveDeepLinkedSession(initialSessionId, live, history);
    if (hit) {
      setLiveTab(hit.tab);
      setSelected(hit.session);
      setDeepLinkMiss(null);
      setDeepLinkDone(initialSessionId);
    }
  }

  React.useEffect(() => {
    if (!initialSessionId || deepLinkDone === initialSessionId) return;
    if (resolveDeepLinkedSession(initialSessionId, live, history)) return;
    if (liveLoading || histLoading) return;          // lists not in yet
    if (deepLinkFetched.current === initialSessionId) return;
    deepLinkFetched.current = initialSessionId;
    const lifetime = new AbortController();
    api.session(initialSessionId)
      .then((s) => {
        if (lifetime.signal.aborted) return;
        // Show the chip it is under: set plays (the default) or its own.
        setGameType(isTablePlay(s.module_type) ? "" : s.module_type);
        setLiveTab(s.status === "completed" ? "history" : "live");
        setSelected(s);
        setDeepLinkMiss(null);
        setDeepLinkDone(initialSessionId);
      })
      .catch(() => { if (!lifetime.signal.aborted) setDeepLinkMiss(initialSessionId); });
    return () => lifetime.abort();
  }, [initialSessionId, live, history, liveLoading, histLoading, deepLinkDone]);

  // Likewise a listing peek picks its session once, not again on every poll.
  const [peekDone, setPeekDone] = React.useState<typeof listingPeek | null>(null);
  if (listingPeek && peekDone !== listingPeek && shown.length > 0) {
    const agent = listingPeek.agentId;
    const match =
      shown.find((s) => s.participants.some((p) => p === agent || p.includes(agent.slice(-8)))) ??
      shown.find((s) => listingPeek.moduleType && s.module_type === listingPeek.moduleType);
    if (match) setSelected(match);
    setPeekDone(listingPeek);
  }

  // Default: the first live table (or result). Kept while it stays in the list.
  if (useChanged([shown, liveTab, gameType, initialSessionId, listingPeek])
    && !listingPeek && !ladders && (!initialSessionId || deepLinkDone === initialSessionId)) {
    setSelected((cur) => {
      if (cur && shown.some((s) => s.session_id === cur.session_id)) return cur;
      if (cur && cur.session_id === deepLinkDone) return cur;
      return shown.length > 0 ? shown[0] : null;
    });
  }

  const shownId = ladders ? null : selected?.session_id ?? null;
  React.useEffect(() => { onSessionShown?.(shownId); }, [shownId, onSessionShown]);

  const openResult = (id: string) => {
    api.session(id).then((s) => {
      setLiveTab(s.status === "active" || s.status === "waiting" ? "live" : "history");
      setGameType(isTablePlay(s.module_type) ? "" : s.module_type);
      setSelected(s);
    }).catch(() => setDeepLinkMiss(id));
  };

  const tabs = games ? (["live", "history", "ladders"] as const) : (["live", "history"] as const);
  const sidebar = (
    <div>
      <Indent>
        <SegmentTabs
          className="e-segment-tabs--grow"
          options={tabs}
          value={liveTab}
          onChange={(v) => {
            setLiveTab(v);
            setSelected(null);
          }}
          labels={{
            live: live.length > 0 ? `live · ${live.length}` : "live",
            history: games ? "results" : "done",
            ladders: "ladders",
          }}
        />
      </Indent>
      {ladders ? (
        <>
          <div className="e-rail-label">Which game's ladder</div>
          {filters.filter((g) => g.id).map((g) => (
            <FlatRailItem key={g.id} selected={ladderGame === g.id} onClick={() => setLadderGame(g.id)}>
              <Text>{g.label}</Text>
            </FlatRailItem>
          ))}
        </>
      ) : (
        <>
          <div className="e-rail-label">{games ? "Which game" : "Which kind"}</div>
          {filters.map((g) => (
            <FlatRailItem
              key={g.id}
              selected={gameType === g.id}
              keepList
              onClick={() => { setGameType(g.id); setSelected(null); }}
            >
              <Text>{g.label}</Text>
            </FlatRailItem>
          ))}
          <Divider />
          {shown.length === 0 ? (
            <Indent>
              <Text className="e-faint">
                {liveTab === "live"
                  ? games
                    ? "No tables live right now. A table appears here when an agent opens a game or accepts a challenge."
                    : "No deals in progress. A deal appears here when an agent hires a service."
                  : games
                    ? "No finished games yet. Each result lands here, and opens at its winner."
                    : "No finished deals yet."}
              </Text>
            </Indent>
          ) : (
            shown.map((s) => (
              <FlatRailItem
                key={s.session_id}
                selected={selected?.session_id === s.session_id}
                onClick={() => setSelected(s)}
              >
                <SessionRow s={s} showStatus={liveTab === "live"} />
              </FlatRailItem>
            ))
          )}
        </>
      )}
    </div>
  );

  const isChessSelected = selected?.module_type.includes("chess");
  return (
    <ViewBody flush>
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={30}
        initialPane={initialSessionId || listingPeek ? "detail" : "list"}
        backLabel={games ? (ladders ? "back to games" : "back to tables") : "back to deals"}>
        {ladders ? (
          <LaddersPanel moduleType={ladderGame} onOpen={openResult} />
        ) : (
        <div className="e-split-main e-split-main--flush">
          <Indent>
            <ViewStatus loading={pollLoading} error={pollErr} />
          </Indent>
          {/* P36-3: a deep link that resolves to nothing says so — the relay 403s a
              non-participant on a private session, so this covers "gone" and "not yours". */}
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
              <Text className="e-faint">Pick a live or finished one in the list.</Text>
            </Indent>
          ) : null}
          {!selected && !listingPeek && !deepLinkMiss && games ? (
            <Indent>
              {/* quests:v4: the open challenges anyone may take, then the recent results. */}
              <OpenChallenges moduleType={gameType || undefined} />
              {/* With no table open, the recent results — each opens at its end. */}
              <GameResults moduleType={gameType || undefined} onOpen={openResult} />
            </Indent>
          ) : null}
          {selected ? (
            <AskAgent block prompt={gamePrompt(selected)}
              label={selected.module_type === "emporia:service:v1" ? "ask agent about this deal" : "ask agent about this game"} />
          ) : null}
          {selected && isSetPlay(selected.module_type) ? (
            <SetPlayPanel key={selected.session_id} session={selected} />
          ) : selected && isBargain(selected.module_type) ? (
            <BargainPanel key={selected.session_id} session={selected} />
          ) : selected && isChessSelected ? (
            <ChessReplayPanel key={selected.session_id} session={selected} />
          ) : selected ? (
            <>
              {listingPeek ? <ListingPeekBanner peek={listingPeek} /> : null}
              <NonChessSessionPanel key={selected.session_id} session={selected} />
            </>
          ) : null}
        </div>
        )}
      </SidebarLayout>
    </ViewBody>
  );
}
