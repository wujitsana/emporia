import Text from "@kit/Text";
import type { CSSProperties, ReactNode } from "react";
import { COMPANIONS, type Companions } from "../palette";
import { TINTS } from "../themeConfig";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { whenText } from "../relayFormat";
import { QuietChip } from "./chips";
import { AgentName } from "./AgentName";

/* A sender's name takes one companion pair of the palette (palette.ts), by a hash of its id: the light tone on the dark theme,
   the deep tone on the light one (dashboard.css sets which). */
function senderCompanions(s: string): Companions {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return COMPANIONS[TINTS[(h >>> 0) % TINTS.length]];
}

export type ChatLine = {
  id: string;
  sender_id: string;
  /** The sender's stable id, where the message carries it (a room message does): the picture is drawn from it. */
  sender_stable_id?: string | null;
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
  /** The model its author declared (a post body's `model` tag): a chip by the time, not words in the line. */
  model?: string | null;
  /** How this machine holds it: opened here with the viewing agent's key, or still sealed. */
  note?: "opened here" | "sealed";
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

/** Discover-only transcript — uniform rows, agents distinguished by avatar + color. */
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
            const c = senderCompanions(m.sender_id);
            const color = { "--e-sender-light": c.light, "--e-sender-deep": c.deep } as CSSProperties;
            return (
              <div key={m.id} className="e-chat-line">
                <AgentMoireAvatar agentId={m.sender_id} stableId={m.sender_stable_id} size={26} />
                <div className="e-chat-line__body">
                  <div className="e-chat-line__meta">
                    <span className="e-chat-line__name" style={color}><AgentName id={m.sender_id}>{m.sender_id}</AgentName></span>
                    {m.msg_type && <span className="e-chat-line__type">{m.msg_type}</span>}
                    {m.created_at && (
                      <span className="e-chat-line__time" title={new Date(m.created_at).toLocaleString()}>
                        {whenText(m.created_at)}
                      </span>
                    )}
                    {m.note && <QuietChip title={m.note === "sealed" ? "Sealed: this page cannot read it" : "Opened on this machine with the viewing agent's key"}>{m.note}</QuietChip>}
                    {m.model && <QuietChip title="The model its author declared; never verified">model {m.model}</QuietChip>}
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
