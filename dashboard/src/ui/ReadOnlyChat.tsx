import Text from "@components/Text";
import type { ReactNode } from "react";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { whenText } from "../relayFormat";

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
  /** P23-4: Ed25519 signature check against the sender's public key
   *  (integrityCheck.ts's verifyMessageSignature) -- `null`/undefined means
   *  unknown (no signature/nonce on the row, or the sender's key hasn't
   *  loaded yet), not tampered with; only `false` means the check actually
   *  failed. Omitted entirely by a caller that hasn't wired this through. */
  verified?: boolean | null;
  /** P23-4: a nonce seen earlier on this same page (integrityCheck.ts's
   *  markReplayedNonces) -- per-call state only, same standing limitation
   *  the Python-side annotate_messages() documents. */
  replayed?: boolean;
  /** P192: its text was removed — by whom, in what role. */
  removed?: { by?: string; role?: string } | null;
};

type ReadOnlyChatProps = {
  messages: ChatLine[];
  viewerId: string;
  emptyLabel?: string;
  footer?: ReactNode;
  endRef?: React.RefObject<HTMLDivElement | null>;
};

/** P192: how a removed message reads — who removed it, in what role. */
function removedLabel(removed: { by?: string; role?: string } | null): string {
  if (!removed) return "removed";
  return removed.role === "author" ? `removed by ${removed.by ?? "its author"}`
    : `removed by ${removed.by ?? "?"} (${removed.role ?? "?"})`;
}

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
                      <span className="e-chat-line__time" title={new Date(m.created_at).toLocaleString()}>
                        {whenText(m.created_at)}
                      </span>
                    )}
                    {/* P23-4: `undefined`/`null` (not wired up, or nothing to
                        check) renders nothing -- only an actual failed check
                        is shown, so a view that hasn't loaded sender keys yet
                        doesn't flash a false negative. */}
                    {m.verified === true && (
                      <span className="e-faint" style={{ fontSize: 10 }} title="Ed25519 signature verified against the sender's public key">✓ signed</span>
                    )}
                    {m.verified === false && (
                      <span style={{ fontSize: 10, color: "var(--theme-warning, #c33)" }} title="Signature does not match this message's stored content -- may have been altered after the sender sent it">✗ unverified</span>
                    )}
                    {m.replayed && (
                      <span style={{ fontSize: 10, color: "var(--theme-warning, #c33)" }} title="This message's nonce was already seen earlier on this page -- a possible replay">⚠ replay</span>
                    )}
                  </div>
                  <div className="e-chat-line__content">
                    {m.removed !== undefined
                      ? <span className="e-faint">{removedLabel(m.removed)}</span>
                      : m.content}
                  </div>
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
