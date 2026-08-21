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

  // P24-1/2 (W1/W2): `verified: true` no longer means one fixed thing --
  // "genesis" (this call walked the whole room) and "pinned_checkpoint"
  // (this call trusted an already-established anchor and only checked what
  // changed since) are both real, both honest, but overclaiming either as
  // the other is exactly the kind of false confidence SECURITY.md flags
  // this badge as existing to avoid. Surfaced in the tooltip, not the badge
  // text itself -- the label stays short; the distinction is one hover away.
  const scopeNote =
    result.chain_verified_scope === "genesis"
      ? "verified from true genesis this call"
      : result.chain_verified_scope === "pinned_checkpoint"
        ? "verified back to a previously-pinned checkpoint (genesis was verified when that checkpoint was first pinned)"
        : null;
  const replayNote =
    result.replayed_nonce_count > 0
      ? ` — ${result.replayed_nonce_count} duplicate nonce(s) found (scan scope: ${result.replay_scan_scope})`
      : "";

  return (
    <Text
      className={result.verified ? undefined : "e-faint"}
      style={{
        fontSize: 11,
        color: result.verified ? "var(--theme-focused-foreground)" : "var(--theme-warning, #c33)",
      }}
      title={
        `${result.reason} — independently recomputed from this room's own message history and ` +
        `checked against a head pinned in this browser` +
        (scopeNote ? ` (${scopeNote})` : "") + replayNote
      }
    >
      {result.verified ? `✓ chain verified (${result.message_count})` : "✗ chain unverified"}
      {result.replayed_nonce_count > 0 ? " ⚠" : ""}
    </Text>
  );
}
