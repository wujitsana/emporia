import Text from "@components/Text";
import { useInterval } from "../hooks";
import { api } from "../api";

/**
 * Hash-chain audit verification badge — proves the tamper-evident public
 * receipt log (session_audit.py) actually verifies, not just that hashing
 * happens somewhere server-side.
 */
export function AuditBadge({ sessionId }: { sessionId: string }) {
  const [audit] = useInterval(() => api.sessionAudit(sessionId), 10_000, [sessionId]);

  if (!audit || audit.chain.length <= 1) return null;

  const entries = audit.chain.length - 1; // exclude genesis block
  return (
    <Text
      className={audit.verified ? undefined : "e-faint"}
      style={{
        fontSize: 11,
        color: audit.verified ? "var(--theme-focused-foreground)" : undefined,
      }}
      title={`${audit.message} — hash chain: prev_hash:sender:action:payload:signature`}
    >
      {audit.verified ? `✓ chain verified (${entries})` : "✗ chain broken"}
    </Text>
  );
}
