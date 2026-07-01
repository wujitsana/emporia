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
import { ChessReplayStatus, ChessSpeedSlider, ChessTransport } from "../ui/chessControls";
import { RELAY, VIEWER } from "../relayEnv";
import { fetchDashboardCounts, countCaptionForNav, subForNav } from "../dashboardCounts";
import { STARTING_FEN, fenToBoard } from "../fen";
import {
  buildChessReplay,
  chessSessionPlayable,
  chessSides,
  sessionIsActive,
  shortAgent,
} from "../chessReplay";
import { ChessMoveLine, ChessPlayersBar } from "../ui/chessMatch";
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
  const sessions: Session[] = (data?.sessions ?? []).filter(chessSessionPlayable);
  const [selected, setSelected] = useState<Session | null>(null);
  const [history, setHistory] = useState<string[]>([STARTING_FEN]);
  const [sans, setSans] = useState<string[]>([]);
  const [idx, setIdx] = useState(0);
  const [replaying, setReplaying] = useState(false);
  const [speed, setSpeed] = useState(700);
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

  useEffect(() => {
    if (!selected || !selected.module_type.includes("chess")) return;
    const fallback = (selected.state as { board_fen?: string } | undefined)?.board_fen;
    let cancelled = false;
    api.sessionActions(selected.session_id)
      .then((r) => {
        if (cancelled) return;
        const built = buildChessReplay(r.actions, fallback);
        setHistory(built.fens);
        setSans(built.sans);
        setIdx(Math.max(0, built.fens.length - 1));
      })
      .catch(() => {
        if (!cancelled && fallback) {
          setHistory([STARTING_FEN, fallback]);
          setSans([]);
          setIdx(1);
        }
      });
    return () => { cancelled = true; };
  }, [selected?.session_id, selected?.module_type]);

  // Session WebSocket — live tail while active
  useEffect(() => {
    if (!selected || !sessionIsActive(selected.status)) return;
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
        if (f.type === "action_result" && f.new_state?.board_fen) {
          api.sessionActions(selected.session_id)
            .then((r) => {
              const built = buildChessReplay(r.actions, selected.state?.board_fen as string | undefined);
              setHistory(built.fens);
              setSans(built.sans);
              setIdx(Math.max(0, built.fens.length - 1));
            })
            .catch(() => {});
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
  }, [selected?.session_id, selected?.status]);

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
    }, speed);
  }, [history.length, speed]);

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
        sessions.map((s) => {
          const { white, black } = chessSides(s);
          return (
          <FlatRailItem
            key={s.session_id}
            selected={selected?.session_id === s.session_id}
            onClick={() => setSelected(s)}
          >
            <RowSpaceBetween>
              <Text>{shortAgent(white, 10)} v {shortAgent(black, 10)}</Text>
              <Text className="e-dim e-status-txt">{s.status}</Text>
            </RowSpaceBetween>
            <Text className="e-dim">
              {s.module_type.replace("emporia:", "").replace(":v1", "")} · {s.step_number} plies
            </Text>
          </FlatRailItem>
          );
        })
      )}
    </>
  );

  return (
    <ViewBody flush>
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={26}>
        <div className="e-split-main">
          <Indent>
            <ViewStatus loading={loading} error={error} />
          </Indent>
          {!selected ? null : isChess ? (
            <div className="e-chess-main">
              <div className="e-chess-column">
                <div className="e-chess-stage">
                  <Chessboard board={board} key={`${selected.session_id}-${idx}-${history[idx] ?? ""}`} />
                  <ChessTransport
                    onPrev={() => { stopReplay(); setIdx(Math.max(0, idx - 1)); }}
                    onNext={() => { stopReplay(); setIdx(Math.min(history.length - 1, idx + 1)); }}
                    onReplayToggle={replaying ? stopReplay : startReplay}
                    replaying={replaying}
                    onLatest={() => {
                      stopReplay();
                      setIdx(history.length - 1);
                    }}
                    status={
                      <ChessReplayStatus live={wsLive} idx={idx} total={history.length} />
                    }
                  />
                  <ChessSpeedSlider speed={speed} onSpeed={setSpeed} />
                </div>
                {sans.length > 0 ? (
                  <div className="e-chess-moves-block">
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
                <ChessPlayersBar session={selected} />
                <RowSpaceBetween className="e-chess-meta">
                  <Text className="e-dim">
                    turn · {selected.current_agent}{" "}
                    {selected.current_agent === chessSides(selected).white ? "♔" : "♚"}
                  </Text>
                  <Text className="e-dim">{selected.status}</Text>
                </RowSpaceBetween>
              </div>
            </div>
          ) : (
            <Indent>
              <MetaGrid
                rows={[
                  ["Session", `…${selected.session_id.slice(-16)}`],
                  ["Module", selected.module_type.replace("emporia:", "")],
                  ["Status", selected.status],
                  ["Step", String(selected.step_number)],
                  ["Turn", selected.current_agent],
                ]}
              />
            </Indent>
          )}
        </div>
      </SidebarLayout>
    </ViewBody>
  );
}
