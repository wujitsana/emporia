import ActionButton from "@components/ActionButton";
import ActionListItem from "@components/ActionListItem";
import Badge from "@components/Badge";
import Card from "@components/Card";
import Chessboard from "@components/Chessboard";
import Divider from "@components/Divider";
import Indent from "@components/Indent";
import Input from "@components/Input";
import Message from "@components/Message";
import MessageViewer from "@components/MessageViewer";
import Navigation from "@components/Navigation";
import RowSpaceBetween from "@components/RowSpaceBetween";
import Select from "@components/Select";
import SidebarLayout from "@components/SidebarLayout";
import SimpleTable from "@components/SimpleTable";
import Text from "@components/Text";
import Window from "@components/Window";
import AlertBanner from "@components/AlertBanner";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type {
  AgentProfile,
  AgoraPost,
  AgoraTopic,
  DMMessage,
  DMThread,
  EmporiaEvent,
  Listing,
  Room,
  Session,
  SessionAction,
} from "../api";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { PaymentsFeesSection } from "../PaymentsFeesSection";
import { EMPTY_SEED_HINT } from "../navigation";
import { FlatRailItem } from "../ui/FlatRailItem";
import { SegmentTabs } from "../ui/SegmentTabs";
import {
  ChessSpeedSlider,
  ChessTransport,
  LiveMark,
  MoveIndex,
} from "../ui/chessControls";
import { RELAY, VIEWER } from "../relayEnv";
import { fetchDashboardCounts, countCaptionForNav, subForNav } from "../dashboardCounts";
import { STARTING_FEN, fenToBoard } from "../fen";
import type { Navigate } from "../navigation";
import { ViewStatus } from "../ui/ViewStatus";
import { ViewBody } from "../ui/layout";
import { eventDetail, viewForEvent } from "../eventNav";
import type { NavOpts } from "../eventNav";
import { ListingPeekBanner } from "../ui/ListingPeekBanner";
import { AuditBadge } from "../ui/AuditBadge";
import { NAV } from "../navConfig";
import {
  type GlobalEvent,
  type RelayHealth,
  type WsMessage,
  toWsUrl,
  useInterval,
  useRoomWs,
} from "../hooks";

export const GAME_TYPES = [
  { id: "", label: "all" },
  { id: "emporia:chess:v1", label: "chess" },
  { id: "emporia:service:v1", label: "service" },
  { id: "emporia:code-review:v1", label: "code review" },
  { id: "emporia:research:v1", label: "research" },
] as const;

export function ChessReplayPanel({ session }: { session: Session }) {
  const [actions, setActions] = React.useState<SessionAction[]>([]);
  const [idx, setIdx] = React.useState(0);
  const [replaying, setReplaying] = React.useState(false);
  const [speed, setSpeed] = React.useState(700);
  const [wsLive, setWsLive] = React.useState(false);
  const [liveFens, setLiveFens] = React.useState<string[]>([]);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // For completed sessions: fetch recorded actions
  useEffect(() => {
    if (session.status !== "active") {
      api.sessionActions(session.session_id)
        .then((r) => {
          setActions(r.actions);
          setIdx(Math.max(0, r.actions.length - 1));
        })
        .catch(() => {});
    }
  }, [session.session_id, session.status]);

  // For active sessions: subscribe via WS
  useEffect(() => {
    if (session.status !== "active") return;
    const initialFen = (session.state as any)?.board_fen ?? STARTING_FEN;
    setLiveFens([initialFen]);

    const base = toWsUrl(RELAY);
    let closed = false;
    let retryMs = 1_000;
    let ws: WebSocket;

    const connect = () => {
      if (closed) return;
      ws = new WebSocket(`${base}/ws/sessions/${session.session_id}`);
      ws.onopen = () => { setWsLive(true); retryMs = 1_000; };
      ws.onclose = () => {
        setWsLive(false);
        if (!closed) setTimeout(connect, (retryMs = Math.min(retryMs * 2, 30_000)));
      };
      ws.onmessage = (e) => {
        const f = JSON.parse(e.data);
        if (f.type === "init") {
          const fen = f.session?.state?.board_fen;
          if (fen) setLiveFens([fen]);
        } else if (f.type === "action_result" && f.new_state?.board_fen) {
          setLiveFens((h) => [...h, f.new_state.board_fen]);
          setIdx((i) => i + 1);
        }
      };
    };
    connect();
    const pingId = setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) ws.send('{"type":"ping"}');
    }, 20_000);
    return () => { closed = true; clearInterval(pingId); ws?.close(); };
  }, [session.session_id, session.status]);

  const fens: string[] = session.status === "active"
    ? liveFens
    : [STARTING_FEN, ...actions
        .filter((a) => a.result?.new_state?.board_fen)
        .map((a) => a.result!.new_state!.board_fen as string)];

  const board = fenToBoard(fens[idx] ?? STARTING_FEN);

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
    <Indent>
      <br />
      <RowSpaceBetween>
        <div>
          <Text style={{ fontSize: 12, fontFamily: "var(--font-family-mono)" }}>
            …{session.session_id.slice(-16)}
          </Text>
          <br />
          <Text className="e-dim">
            {session.participants.join(" vs ")}
          </Text>
        </div>
        <div style={{ textAlign: "right" }}>
          <LiveMark live={wsLive} />
          <br />
          <MoveIndex idx={idx} total={fens.length} />
          <br />
          <AuditBadge sessionId={session.session_id} />
        </div>
      </RowSpaceBetween>
      <br />
      <Chessboard board={board} />
      <br />
      <ChessTransport
        onPrev={() => { stopReplay(); setIdx(Math.max(0, idx - 1)); }}
        onNext={() => { stopReplay(); setIdx(Math.min(fens.length - 1, idx + 1)); }}
        onReplayToggle={replaying ? stopReplay : startReplay}
        replaying={replaying}
        onLatest={() => { stopReplay(); setIdx(fens.length - 1); }}
        latestLabel="latest"
      />
      <br />
      <ChessSpeedSlider speed={speed} onSpeed={setSpeed} />
      {actions.length > 0 && (
        <>
          <br />
          <Divider />
          <br />
          <br />
          <div style={{ fontSize: 11, fontFamily: "var(--font-family-mono)", opacity: 0.7, lineHeight: 1.6 }}>
            {actions.map((a, i) => (
              <span
                key={a.action_id}
                onClick={() => { stopReplay(); setIdx(i + 1); }}
                style={{
                  cursor: "pointer",
                  marginRight: 8,
                  color: i + 1 === idx ? "var(--theme-focused-foreground)" : "inherit",
                }}
              >
                {i % 2 === 0 ? `${Math.floor(i / 2) + 1}.` : ""}{String(a.payload?.move ?? a.action_type).slice(0, 5)}
              </span>
            ))}
          </div>
        </>
      )}
    </Indent>
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
          <Text className="e-dim e-status-txt">{session.status}</Text>
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
}: {
  initialSessionId: string | null;
  initialGameModule: string | null;
  listingPeek: NavOpts["listingPeek"] | null;
}) {
  const [gameType, setGameType] = React.useState("");
  const [liveTab, setLiveTab] = React.useState<"live" | "history">("live");
  const [liveSessions, liveLoading, liveErr] = useInterval(
    () => api.sessions({ module_type: gameType || undefined, status: "active" }),
    6_000,
    [gameType],
  );
  const [historySessions, histLoading, histErr] = useInterval(
    () => api.sessions({ module_type: gameType || undefined, status: "complete" }),
    10_000,
    [gameType],
  );
  const [selected, setSelected] = React.useState<Session | null>(null);

  const live = liveSessions?.sessions ?? [];
  const history = historySessions?.sessions ?? [];
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

  React.useEffect(() => {
    if (!initialSessionId || shown.length === 0) return;
    const found = shown.find((s) => s.session_id === initialSessionId);
    if (found) setSelected(found);
  }, [initialSessionId, shown]);

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
        shown.map((s) => (
          <FlatRailItem
            key={s.session_id}
            selected={selected?.session_id === s.session_id}
            onClick={() => setSelected(s)}
          >
            <RowSpaceBetween>
              <Text>…{s.session_id.slice(-10)}</Text>
              {liveTab === "live" && <Text className="e-dim e-status-txt">live</Text>}
            </RowSpaceBetween>
            <Text className="e-dim">
              {s.module_type.replace("emporia:", "").replace(":v1", "")} · {s.step_number}m
            </Text>
            <Text className="e-faint">
              {s.participants.slice(0, 2).map((p) => p.slice(0, 10)).join(" v ")}
            </Text>
          </FlatRailItem>
        ))
      )}
    </div>
  );

  return (
    <ViewBody
      flush
      toolbar={live.length > 0 ? <span className="e-dim e-status-txt">live · {live.length}</span> : undefined}
    >
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={28}>
        <div className="e-split-main">
          <Indent>
            <ViewStatus loading={pollLoading} error={pollErr} />
          </Indent>
          {listingPeek && !selected ? <ListingPeekBanner peek={listingPeek} /> : null}
          {!selected && listingPeek ? (
            <Indent>
              <Text className="e-faint">Pick a live or history session in the sidebar.</Text>
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
