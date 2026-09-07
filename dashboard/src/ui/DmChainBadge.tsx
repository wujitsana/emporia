import Text from "@components/Text";
import { useInterval } from "../hooks";
import { IntegrityChecking, IntegrityUnknown } from "./IntegrityState";
import { api } from "../api";
import { RELAY } from "../relayEnv";
import { verifyDmChainFromRelay } from "../integrityCheck";

/**
 * P32-2: the DM counterpart of RoomChainBadge, and the last leg of P28-4 —
 * which shipped the DM hash chain, and P30-2, which gave Python clients an
 * independent verifier. Same substance as the room badge, deliberately not the
 * shallow version: it does not render the relay's own `verified` self-report,
 * it recomputes the chain from the thread's own message history and checks it
 * against a head pinned in this browser the last time the thread verified
 * cleanly. The relay does not hold that pin, which is the whole point.
 *
 * 60s poll, matching RoomChainBadge — this does real work (a paginated fetch
 * plus a SHA-256 per message via Web Crypto), not one cheap read.
 */
export function DmChainBadge({ threadId, viewerId }: { threadId: string; viewerId: string }) {
  const [result, loading, error] = useInterval(
    () =>
      verifyDmChainFromRelay(
        RELAY,
        threadId,
        api.dmChain,
        (tid, opts) => api.dmMessagesPage(tid, viewerId, opts),
      ),
    60_000,
    [threadId, viewerId],
  );

  // P68: see IntegrityState.tsx — a check that could not run is not the same
  // as a chain with nothing in it, and used to render identically.
  if (error && !result) return <IntegrityUnknown what="chain" detail={error} />;
  if (loading && !result) return <IntegrityChecking what="chain" />;
  if (!result) return null;
  // Nothing chained and nothing older: an untouched thread, no claim to make.
  if (result.message_count === 0 && result.unchained_prefix === 0) return null;

  // The scope distinction is load-bearing here in a way it isn't for rooms:
  // "chained_start" means the walk reached the beginning of the CHAIN, not the
  // thread's first message, because P28-4 deliberately left pre-existing rows
  // unchained rather than backfilling a history the relay can't attest. A
  // badge reading a bare "verified" over a thread with an unchained prefix
  // would claim coverage it doesn't have, so the count is shown inline rather
  // than buried in the tooltip.
  const scopeNote =
    result.chain_verified_scope === "genesis"
      ? "verified from the thread's first message this call"
      : result.chain_verified_scope === "chained_start"
        ? `verified back to the start of the chain; ${result.unchained_prefix} earlier message(s) predate it and are not covered`
        : result.chain_verified_scope === "pinned_checkpoint"
          ? "verified back to a previously-pinned checkpoint (earlier history was verified when that checkpoint was first pinned)"
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
        `${result.reason} — independently recomputed from this thread's own message history ` +
        `and checked against a head pinned in this browser` +
        (scopeNote ? ` (${scopeNote})` : "") + replayNote
      }
    >
      {result.verified ? `\u2713 chain verified (${result.message_count})` : "\u2717 chain unverified"}
      {result.verified && result.unchained_prefix > 0 ? ` +${result.unchained_prefix} unchained` : ""}
      {result.replayed_nonce_count > 0 ? " \u26a0" : ""}
    </Text>
  );
}
