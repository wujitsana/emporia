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
import { ChessTransport, LiveMark, MoveIndex } from "../ui/chessControls";
import { RELAY, VIEWER } from "../relayEnv";
import { fetchDashboardCounts, countCaptionForNav, subForNav } from "../dashboardCounts";
import { STARTING_FEN, fenToBoard } from "../fen";
import type { Navigate } from "../navigation";
import { ViewStatus } from "../ui/ViewStatus";
import { ViewBody } from "../ui/layout";
import { MetaGrid } from "../ui/cards";
import { eventDetail, viewForEvent } from "../eventNav";
import { NAV } from "../navConfig";
import {
  type GlobalEvent,
  type RelayHealth,
  type WsMessage,
  toWsUrl,
  useInterval,
  useRoomWs,
} from "../hooks";

export function SessionsView({
  refreshTrigger,
  initialSessionId,
}: {
  refreshTrigger: number;
  initialSessionId: string | null;
}) {
  const [data, loading, error] = useInterval(() => api.sessions(), 5_000, [refreshTrigger]);
  const sessions: Session[] = data?.sessions ?? [];
  const [selected, setSelected] = useState<Session | null>(null);
  const [history, setHistory] = useState<string[]>([STARTING_FEN]);
  const [idx, setIdx] = useState(0);
  const [replaying, setReplaying] = useState(false);
  const [wsLive, setWsLive] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (initialSessionId || sessions.length === 0) return;
    setSelected((cur) => cur ?? sessions[0]);
  }, [sessions, initialSessionId]);

  // Auto-select when navigated here with an ID
  useEffect(() => {
    if (!initialSessionId || !sessions.length) return;
    const found = sessions.find((s) => s.session_id === initialSessionId);
    if (found && found.session_id !== selected?.session_id) setSelected(found);
  }, [initialSessionId, sessions]);

  // Session WebSocket — reconnects; seeds FEN from init frame
  useEffect(() => {
    if (!selected) return;
    setHistory([STARTING_FEN]);
    setIdx(0);
    setWsLive(false);

    const base = toWsUrl(RELAY);
    let closed = false;
    let retryMs = 1_000;
    let ws: WebSocket;

    const connect = () => {
      if (closed) return;
      ws = new WebSocket(`${base}/ws/sessions/${selected.session_id}`);
      ws.onopen = () => { setWsLive(true); retryMs = 1_000; };
      ws.onclose = () => {
        setWsLive(false);
        if (!closed) setTimeout(connect, (retryMs = Math.min(retryMs * 2, 30_000)));
      };
      ws.onmessage = (e) => {
        const f = JSON.parse(e.data);
        if (f.type === "init") {
          // Seed current board position from the relay's init snapshot
          const fen = f.session?.state?.board_fen;
          if (fen) { setHistory([fen]); setIdx(0); }
        } else if (f.type === "action_result" && f.new_state?.board_fen) {
          setHistory((h) => [...h, f.new_state.board_fen]);
          setIdx((i) => i + 1);
        } else if (f.type === "session_completed") {
          setWsLive(false);
        }
      };
    };

    connect();
    const pingId = setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) ws.send('{"type":"ping"}');
    }, 20_000);

    return () => {
      closed = true;
      clearInterval(pingId);
      ws?.close();
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [selected?.session_id]);

  const startReplay = useCallback(() => {
    setIdx(0);
    setReplaying(true);
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = setInterval(() => {
      setIdx((i) => {
        if (i >= history.length - 1) {
          clearInterval(timerRef.current!);
          setReplaying(false);
          return i;
        }
        return i + 1;
      });
    }, 700);
  }, [history.length]);

  const stopReplay = () => {
    if (timerRef.current) clearInterval(timerRef.current);
    setReplaying(false);
  };

  const board = fenToBoard(history[idx] ?? STARTING_FEN);
  const isChess = selected?.module_type.includes("chess");

  const sidebar = (
    <>
      {sessions.length === 0 ? (
        <Indent><Text className="e-faint">No active sessions</Text></Indent>
      ) : (
        sessions.map((s) => (
          <FlatRailItem
            key={s.session_id}
            selected={selected?.session_id === s.session_id}
            onClick={() => setSelected(s)}
          >
            <RowSpaceBetween>
              <Text>…{s.session_id.slice(-10)}</Text>
              <Text className="e-dim e-status-txt">{s.status}</Text>
            </RowSpaceBetween>
            <Text className="e-dim">
              {s.module_type.replace("emporia:", "").replace(":v1", "")} · step {s.step_number}
            </Text>
          </FlatRailItem>
        ))
      )}
    </>
  );

  return (
    <ViewBody flush toolbar={wsLive && selected ? <LiveMark live /> : undefined}>
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={26}>
        <Indent>
          <br />
          <ViewStatus loading={loading} error={error} />
          {!selected ? null : isChess ? (
            <>
              <RowSpaceBetween>
                <Text style={{ fontSize: 13 }}>…{selected.session_id.slice(-14)}</Text>
                <MoveIndex idx={idx} total={history.length} />
              </RowSpaceBetween>
              <br />
              <Chessboard board={board} />
              <br />
              <ChessTransport
                onPrev={() => setIdx(Math.max(0, idx - 1))}
                onNext={() => setIdx(Math.min(history.length - 1, idx + 1))}
                onReplayToggle={replaying ? stopReplay : startReplay}
                replaying={replaying}
                onLatest={() => { stopReplay(); setIdx(history.length - 1); }}
              />
              <br />
              <RowSpaceBetween>
                <Text className="e-dim">Turn: {selected.current_agent}</Text>
                <Text className="e-dim">{selected.status}</Text>
              </RowSpaceBetween>
            </>
          ) : (
            <MetaGrid
              rows={[
                ["Session", `…${selected.session_id.slice(-16)}`],
                ["Module", selected.module_type.replace("emporia:", "")],
                ["Status", selected.status],
                ["Step", String(selected.step_number)],
                ["Turn", selected.current_agent],
              ]}
            />
          )}
          <br />
        </Indent>
      </SidebarLayout>
    </ViewBody>
  );
}
