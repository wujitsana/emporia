import { FlatRailItem } from "../ui/FlatRailItem";
import { LiveMark } from "../ui/chessControls";
import { ReadOnlyChat } from "../ui/ReadOnlyChat";
import { RELAY, VIEWER } from "../relayEnv";
import { ViewStatus } from "../ui/ViewStatus";
import { ViewBody } from "../ui/layout";
import { EMPTY_SEED_HINT } from "../navigation";
import { api } from "../api";
import type { Room } from "../api";
import React, { useEffect, useRef, useState } from "react";
import Text from "@components/Text";
import SidebarLayout from "@components/SidebarLayout";
import RowSpaceBetween from "@components/RowSpaceBetween";
import Indent from "@components/Indent";
import { useInterval, useRoomWs, type WsMessage } from "../hooks";
import type { Navigate } from "../navigation";

export function RoomsView({ initialRoomId, navigate }: { initialRoomId: string | null; navigate?: Navigate }) {
  const [data, loading, error] = useInterval(() => api.rooms(VIEWER), 8_000);
  const rooms: Room[] = data?.rooms ?? [];
  const [selected, setSelected] = useState<Room | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const { messages, connected: wsConnected } = useRoomWs(RELAY, selected?.room_id ?? null, VIEWER);

  useEffect(() => {
    if (initialRoomId || rooms.length === 0) return;
    setSelected((cur) => cur ?? rooms[0]);
  }, [rooms, initialRoomId]);

  useEffect(() => {
    if (!initialRoomId || !rooms.length) return;
    const found = rooms.find((r) => r.room_id === initialRoomId);
    if (found && found.room_id !== selected?.room_id) setSelected(found);
  }, [initialRoomId, rooms]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length]);

  const roomSidebar = (
    <>
      {rooms.length === 0 ? (
        <Indent>
          <Text className="e-faint">{loading ? "Loading rooms…" : error ? "" : EMPTY_SEED_HINT}</Text>
        </Indent>
      ) : (
        rooms.map((r) => (
          <FlatRailItem
            key={r.room_id}
            selected={selected?.room_id === r.room_id}
            onClick={() => setSelected(r)}
          >
            <RowSpaceBetween>
              <Text style={{ fontSize: 13 }}>{r.name}</Text>
              <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
                {r.encrypted && <span title="Encrypted" style={{ fontSize: 11 }}>🔒</span>}
                {r.entry_fee_cents > 0 && (
                  <span className="e-gate-badge e-gate-badge--paid" title="paid entry">
                    ${(r.entry_fee_cents / 100).toFixed(2)}
                  </span>
                )}
                <Text className="e-dim e-status-txt">{r.room_type}</Text>
              </div>
            </RowSpaceBetween>
            <Text className="e-dim">
              {r.gate_type} · {r.members?.length ?? r.member_count ?? "?"} members
            </Text>
            {r.linked_session_id && (
              <button
                type="button"
                className="e-room-session-link"
                title={`Linked session: ${r.linked_session_id}`}
                onClick={(e) => {
                  e.stopPropagation();
                  navigate?.("sessions", { sessionId: r.linked_session_id! });
                }}
              >
                ⊙ session …{r.linked_session_id.slice(-8)}
              </button>
            )}
          </FlatRailItem>
        ))
      )}
    </>
  );

  const chatLines = (messages as WsMessage[]).map((m) => ({
    id: m.message_id,
    sender_id: m.sender_id,
    content: m.content,
    msg_type: m.msg_type,
    created_at: m.created_at,
  }));

  return (
    <ViewBody flush toolbar={wsConnected && selected ? <LiveMark live /> : undefined}>
      <SidebarLayout sidebar={roomSidebar} defaultSidebarWidth={26}>
        <div className="e-split-main">
          <ViewStatus loading={loading} error={error} />
          {selected ? (
            <ReadOnlyChat
              messages={chatLines}
              viewerId={VIEWER}
              endRef={endRef}
              footer={
                <Text className="e-faint">Discover · agent room traffic (read-only)</Text>
              }
            />
          ) : null}
        </div>
      </SidebarLayout>
    </ViewBody>
  );
}