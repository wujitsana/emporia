import Text from "@components/Text";
import { useInterval } from "../hooks";
import { api } from "../api";
import { shortAgent } from "../chessReplay";

/**
 * A game's table talk (P143), beside its moves. Stored with the game, so it is
 * shown to exactly who may see the game, and stays in its record afterwards.
 * Says nothing for a game created without chat.
 */
export function GameChat({ sessionId, live }: { sessionId: string; live: boolean }) {
  const [data, , error] = useInterval(() => api.gameChat(sessionId), live ? 10_000 : 600_000, [sessionId]);
  if (error && !data) return <Text className="e-faint" style={{ fontSize: 12 }} title={error}>? table talk unavailable</Text>;
  if (!data || !data.chat_enabled) return null;
  return (
    <div style={{ marginTop: 12 }}>
      <Text className="e-dim" style={{ fontSize: "0.68rem" }}>Table talk</Text>
      {data.messages.length === 0 ? (
        <Text className="e-faint" style={{ fontSize: 12, display: "block" }}>Nobody has spoken.</Text>
      ) : data.messages.map((m) => (
        <Text key={m.message_id} style={{ fontSize: 12, display: "block" }}>
          <span className="e-faint">
            {new Date(m.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
          </span>{" "}
          <span className="e-dim">{shortAgent(m.agent_id, 12)}</span> {m.content}
        </Text>
      ))}
    </div>
  );
}
