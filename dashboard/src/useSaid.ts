import { useEffect, useState } from "react";
import { api, looksLikeEncryptedEnvelope } from "./api";
import type { GameChatMessage } from "./api";
import { useInterval } from "./hooks";
import { openSealed } from "./sealedSession";

/**
 * The lines players said with their moves, for the move list and the step view. They are read with the
 * game (`GET /sessions/{id}/chat`, stored with `step`), so they have exactly the game's visibility. A private
 * game's lines are sealed: opened here with the viewer's key, else shown as "sealed".
 */
export function useSaid(sessionId: string, live: boolean): { messages: GameChatMessage[]; opened: Record<string, string>; enabled: boolean; failed: boolean } {
  const [data, , error] = useInterval(() => api.gameChat(sessionId), live ? 10_000 : 600_000, [sessionId]);
  const [opened, setOpened] = useState<Record<string, string>>({});
  useEffect(() => {
    const items = (data?.messages ?? []).filter((m) => looksLikeEncryptedEnvelope(m.content))
      .map((m) => ({ id: m.message_id, field: "chat", content: m.content }));
    if (items.length) openSealed(sessionId, items).then(setOpened).catch(() => setOpened({}));
  }, [data, sessionId]);
  return { messages: data?.chat_enabled ? data.messages : [], opened, enabled: !!data?.chat_enabled, failed: !!error && !data };
}
