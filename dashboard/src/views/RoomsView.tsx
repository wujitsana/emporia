import { FlatRailItem } from "../ui/FlatRailItem";
import { AcceptsTag } from "../ui/PayTag";
import { LiveMark } from "../ui/chessControls";
import { ReadOnlyChat } from "../ui/ReadOnlyChat";
import { RoomChainBadge } from "../ui/RoomChainBadge";
import { RELAY, VIEWER_ID } from "../relayEnv";
import { ViewStatus } from "../ui/ViewStatus";
import { ViewBody } from "../ui/layout";
import { EMPTY_SEED_HINT } from "../navigation";
import { api, dmDisplayContent } from "../api";
import type { Room } from "../api";
import React, { useEffect, useMemo, useRef, useState } from "react";
import Text from "@components/Text";
import SidebarLayout from "@components/SidebarLayout";
import RowSpaceBetween from "@components/RowSpaceBetween";
import Indent from "@components/Indent";
import { useInterval, useRoomWs, useSenderPubkeys, type WsMessage } from "../hooks";
import type { Navigate } from "../navigation";
import { markReplayedNonces, verifyMessageSignature } from "../integrityCheck";

export function RoomsView({ initialRoomId, navigate }: { initialRoomId: string | null; navigate?: Navigate }) {
  const [data, loading, error] = useInterval(() => api.rooms(), 8_000);
  // P41-2: memoised. `data?.rooms ?? []` builds a new array every render, so
  // every effect depending on it re-ran on every render — which is what made the
  // deep-link effect below snap the selection back (see its own note).
  const rooms: Room[] = useMemo(() => data?.rooms ?? [], [data]);
  const [selected, setSelected] = useState<Room | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const { messages, connected: wsConnected } = useRoomWs(
    RELAY, selected?.room_id ?? null, VIEWER_ID,
    (id) => api.roomMessages(id).then((r) => r.messages as unknown as WsMessage[]),
  );

  // P23-4: pubkeys needed for verifyMessageSignature() -- fetched on demand,
  // one round trip per distinct sender seen, cached for the life of the view
  // rather than the room (a sender's key doesn't change per-room). Missing
  // entirely for a sender whose fetch is still in flight or failed, which
  // verifyMessageSignature() already treats as "unknown" (undefined pubkey),
  // not "tampered with".
  const pubkeys = useSenderPubkeys(
    (messages as WsMessage[]).map((m) => m.sender_id),
    (id) => api.agentProfile(id).then((a) => a.public_key_hex || "").catch(() => ""),
  );

  useEffect(() => {
    if (initialRoomId || rooms.length === 0) return;
    setSelected((cur) => cur ?? rooms[0]);
  }, [rooms, initialRoomId]);

  /* P41-2: resolve the deep link ONCE per id.
     This used to run on `[initialRoomId, rooms]`, and `rooms` was a fresh array
     every render, so the effect ran after every render and re-applied
     `initialRoomId` each time — clicking a different room in the sidebar set it,
     the effect immediately set it back, and a linked room could not be navigated
     away from. The ref is what makes "follow the link" a one-time act instead of
     a standing instruction. Same shape as the session (P36-3) and event (P40-3)
     deep links. */
  const deepLinkApplied = useRef<string | null>(null);
  useEffect(() => {
    if (!initialRoomId || !rooms.length) return;
    if (deepLinkApplied.current === initialRoomId) return;
    const found = rooms.find((r) => r.room_id === initialRoomId);
    if (found) {
      deepLinkApplied.current = initialRoomId;
      setSelected(found);
    }
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
                  <>
                    <span className="e-gate-badge e-gate-badge--paid" title="paid entry">
                      ${(r.entry_fee_cents / 100).toFixed(2)}
                    </span>
                    <AcceptsTag move="entry" />
                  </>
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

  // P23-4: replay detection needs the raw (undisplayed) content/nonce, and
  // must run over the whole page before mapping to ChatLine -- markReplayedNonces
  // tracks nonces across the array, so it has to see every message at once,
  // not be called per-row.
  const roomId = selected?.room_id ?? "";
  const annotated = markReplayedNonces(messages as WsMessage[]);
  const chatLines = annotated.map((m) => ({
    id: m.message_id,
    sender_id: m.sender_id,
    // P18-6: an encrypted room's messages are envelopes. The dashboard has no key
    // access by design and cannot decrypt — the honest render is a lock, not base64.
    // Same helper the DM views use; the shape is identical, so the name is generic
    // rather than dm-specific in spirit even though it kept its original name.
    content: dmDisplayContent(m.content),
    msg_type: m.msg_type,
    created_at: m.created_at,
    // P23-4: verified against the *stored* content (m.content, pre-display-
    // transform) — for an encrypted room that's the envelope, which is what
    // was actually signed; verifying against dmDisplayContent()'s output
    // (the 🔒 placeholder) would never match any real signature.
    // P192: a removed message has no text to check; its removal is shown instead.
    verified: m.deleted_at ? null : verifyMessageSignature("room", roomId, m, pubkeys[m.sender_id] || ""),
    replayed: m.replayed,
    removed: m.deleted_at ? (m.removed ?? null) : undefined,
  }));

  return (
    <ViewBody
      flush
      toolbar={
        selected ? (
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            {wsConnected && <LiveMark live />}
            {/* P24-5 (W5, F2): key forces a remount on room switch so the badge's
                own polling state can't briefly render the previous room's verdict
                against the new room's id -- useInterval itself intentionally does
                not reset on a dep change (EventsView/ListingsView rely on that to
                avoid blanking on a manual refresh), so the reset has to happen here. */}
            <RoomChainBadge key={selected.room_id} roomId={selected.room_id} />
          </div>
        ) : undefined
      }
    >
      <SidebarLayout sidebar={roomSidebar} defaultSidebarWidth={26}>
        <div className="e-split-main">
          <ViewStatus loading={loading} error={error} />
          {selected ? (
            <ReadOnlyChat
              messages={chatLines}
              viewerId={VIEWER_ID ?? ""}
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