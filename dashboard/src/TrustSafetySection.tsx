import Text from "@components/Text";
import { useInterval } from "./hooks";
import { api } from "./api";
import { SlimCard } from "./ui/cards";

/**
 * NeMo guardrails + Proof-of-Reasoning anti-cheat — live counters.
 * Every inbound action runs guardrails -> Ed25519 signature -> PoR before the
 * module ever sees it; this panel makes that pipeline visible instead of
 * leaving it as a server-side implementation detail.
 */
export function TrustSafetySection() {
  const [stats] = useInterval(() => api.safetyStats(), 10_000);

  return (
    <SlimCard
      title="Trust & Safety"
      foot="NeMo guardrails scan every payload for prompt injection; Proof-of-Reasoning blocks engine-assisted moves. Counters are live since this relay started."
    >
      <div className="e-kpi-row">
        <div className="e-kpi">
          <span className="e-kpi__val">{stats?.guardrails_mode ?? "—"}</span>
          <span className="e-kpi__lbl">guardrails</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{stats?.guardrail_blocks ?? 0}</span>
          <span className="e-kpi__lbl">injections blocked</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{stats?.por_rejections ?? 0}</span>
          <span className="e-kpi__lbl">PoR rejections</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{stats?.unsigned_actions_rejected ?? 0}</span>
          <span className="e-kpi__lbl">unsigned rejected</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{stats?.min_rationale_chars ?? 15}</span>
          <span className="e-kpi__lbl">min rationale</span>
        </div>
      </div>
      {stats?.bot_fingerprints && stats.bot_fingerprints.length > 0 && (
        <Text className="e-dim" style={{ marginTop: 8, fontSize: "0.72rem" }}>
          Bot fingerprints watched: {stats.bot_fingerprints.join(", ")}
        </Text>
      )}
    </SlimCard>
  );
}
