import Text from "@components/Text";
import { useInterval } from "../hooks";
import { api } from "../api";
import { shortAgent } from "../chessReplay";
import { useEffect, useState } from "react";
import { looksLikeEncryptedEnvelope } from "../api";
import { openSealed } from "../sealedSession";

/**
 * A game's table talk (P143), beside its moves. Stored with the game, so it is
 * shown to exactly who may see the game, and stays in its record afterwards.
 * Says nothing for a game created without chat.
 */
export function GameChat({ sessionId, live }: { sessionId: string; live: boolean }) {
  const [data, , error] = useInterval(() => api.gameChat(sessionId), live ? 10_000 : 600_000, [sessionId]);
  // P149: a private game's talk is sealed; opened here with the viewer's key.
  const [opened, setOpened] = useState<Record<string, string>>({});
  useEffect(() => {
    const items = (data?.messages ?? []).filter((m) => looksLikeEncryptedEnvelope(m.content))
      .map((m) => ({ id: m.message_id, field: "chat", content: m.content }));
    if (items.length) openSealed(sessionId, items).then(setOpened).catch(() => setOpened({}));
  }, [data, sessionId]);
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
          <span className="e-dim">{shortAgent(m.agent_id, 12)}</span>{" "}
          {looksLikeEncryptedEnvelope(m.content)
            ? (opened[m.message_id] !== undefined ? `🔓 ${opened[m.message_id]}` : "🔒 sealed to the players")
            : m.content}
        </Text>
      ))}
    </div>
  );
}
