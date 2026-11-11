import Text from "@kit/Text";
import { useInterval } from "../hooks";
import { IntegrityChecking } from "./IntegrityState";
import { api } from "../api";

/**
 * Hash-chain audit verification badge — proves the tamper-evident public
 * receipt log (session_audit.py) actually verifies, not just that hashing
 * happens somewhere server-side.
 */
export function AuditBadge({ sessionId }: { sessionId: string }) {
  const [audit, loading, error] = useInterval(() => api.sessionAudit(sessionId), 10_000, [sessionId]);

  // P68: see IntegrityState.tsx.
  if (error && !audit) return <Text className="e-faint" style={{ fontSize: 11 }} title={`The check did not run, so nothing is claimed either way.\n\n${error}`}>Record not checked on this machine.</Text>;
  if (loading && !audit) return <IntegrityChecking what="audit log" />;
  if (!audit || audit.chain.length <= 1) return null;

  const entries = audit.chain.length - 1; // exclude genesis block
  return (
    <Text
      className={audit.verified ? undefined : "e-faint"}
      style={{
        fontSize: 11,
        color: audit.verified ? "var(--theme-focused-foreground)" : undefined,
      }}
      title={`The relay's own check: ${audit.message}. For a check that does not trust the relay, see "verified here" or run verify(kind="session").`}
    >
      {audit.verified ? `relay: chain ok (${entries})` : "✗ chain broken"}
    </Text>
  );
}
