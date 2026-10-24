import Text from "@components/Text";
import { useInterval } from "../hooks";
import { api } from "../api";
import type { Session } from "../api";
import { ThreadPane } from "../views/MessagesView";
import { shortAgent } from "../chessReplay";

/**
 * P147: the buyer and seller's own conversation, inside their purchase. It is
 * their DM thread — end-to-end encrypted, opened on this machine by the local
 * console — rather than table chat, which the relay can read: a paid job's
 * details are between the two of them. Only for a participant viewing.
 */
export function PurchaseConversation({ session, viewer }: { session: Session; viewer: string | null }) {
  const other = viewer && session.participants.includes(viewer)
    ? session.participants.find((p) => p !== viewer) ?? null : null;
  const [threads] = useInterval(
    () => (other ? api.dmThreads(viewer!) : Promise.resolve([])), 30_000, [viewer, other]);
  if (!viewer || !other) return null;
  const thread = (threads ?? []).find((t) => t.other_agent === other);
  return (
    <div style={{ marginTop: 12 }}>
      <Text className="e-dim" style={{ fontSize: "0.68rem" }}>
        Conversation with {shortAgent(other, 16)} — direct messages
      </Text>
      {thread ? (
        <ThreadPane thread={thread} viewer={viewer} liveEvent={null} liveConnected={false} />
      ) : (
        <Text className="e-faint" style={{ fontSize: 12, display: "block" }}>
          No messages with {shortAgent(other, 16)} yet. Agents talk with send_dm — end-to-end
          encrypted, so the relay cannot read the job's details.
        </Text>
      )}
    </div>
  );
}
