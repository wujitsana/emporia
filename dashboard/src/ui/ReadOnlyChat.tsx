import Text from "@components/Text";
import type { ReactNode } from "react";
import { AgentMoireAvatar } from "../AgentMoireAvatar";

/* 10 well-separated hues — shared with AgentMoireAvatar */
const SENDER_PALETTE = [210, 0, 145, 280, 35, 175, 320, 60, 255, 100] as const;

function senderHue(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return SENDER_PALETTE[(h >>> 0) % SENDER_PALETTE.length];
}

function senderColor(id: string) {
  return `hsl(${senderHue(id)}, 65%, 58%)`;
}

export type ChatLine = {
  id: string;
  sender_id: string;
  content: string;
  msg_type?: string;
  created_at?: string;
};

type ReadOnlyChatProps = {
  messages: ChatLine[];
  viewerId: string;
  emptyLabel?: string;
  footer?: ReactNode;
  endRef?: React.RefObject<HTMLDivElement | null>;
};

/** Discover-only transcript — uniform rows, agents distinguished by avatar + colour. */
export function ReadOnlyChat({
  messages,
  viewerId: _viewerId,
  emptyLabel = "No messages yet",
  footer,
  endRef,
}: ReadOnlyChatProps) {
  return (
    <div className="e-chat-pane">
      <div className="e-chat-messages">
        {messages.length === 0 ? (
          <Text className="e-faint">{emptyLabel}</Text>
        ) : (
          messages.map((m) => {
            const color = senderColor(m.sender_id);
            return (
              <div key={m.id} className="e-chat-line">
                <AgentMoireAvatar agentId={m.sender_id} size={26} />
                <div className="e-chat-line__body">
                  <div className="e-chat-line__meta">
                    <span className="e-chat-line__name" style={{ color }}>{m.sender_id}</span>
                    {m.msg_type && <span className="e-chat-line__type">{m.msg_type}</span>}
                    {m.created_at && (
                      <span className="e-chat-line__time">
                        {new Date(m.created_at).toLocaleTimeString()}
                      </span>
                    )}
                  </div>
                  <div className="e-chat-line__content">{m.content}</div>
                </div>
              </div>
            );
          })
        )}
        {endRef ? <div ref={endRef} /> : null}
      </div>
      {footer ? <div className="e-chat-footer">{footer}</div> : null}
    </div>
  );
}
