import Text from "@components/Text";
import { useInterval } from "../hooks";
import { api } from "../api";
import { RELAY } from "../relayEnv";
import { verifyRoomChainFromRelay } from "../integrityCheck";

/**
 * P23-4: the room-chain analogue of AuditBadge.tsx, but NOT the shallow
 * version SECURITY.md's P21 section deliberately declined to build ("a badge
 * that only surfaced the relay's own `verified` self-report would be the
 * same shallow guarantee AuditBadge already gives for sessions"). This calls
 * `verifyRoomChainFromRelay()`, which independently recomputes the chain
 * from the room's own message history and checks it against a head pinned
 * in this browser's localStorage the last time the room verified cleanly --
 * the same TOFU-pinning property agent_sdk.py's/mcp_server.py's
 * verify_room_chain() give a Python client, now available to the dashboard.
 *
 * 60s poll, not AuditBadge's 10s -- this does real work (a full paginated
 * message fetch + a SHA-256 per message via Web Crypto), not one cheap
 * small-chain read.
 */
export function RoomChainBadge({ roomId }: { roomId: string }) {
  const [result] = useInterval(
    () => verifyRoomChainFromRelay(RELAY, roomId, api.roomChain, api.roomMessages),
    60_000,
    [roomId],
  );

  if (!result || result.message_count === 0) return null;

  return (
    <Text
      className={result.verified ? undefined : "e-faint"}
      style={{
        fontSize: 11,
        color: result.verified ? "var(--theme-focused-foreground)" : "var(--theme-warning, #c33)",
      }}
      title={`${result.reason} — independently recomputed from this room's own message history and checked against a head pinned in this browser`}
    >
      {result.verified ? `✓ chain verified (${result.message_count})` : "✗ chain unverified"}
    </Text>
  );
}
