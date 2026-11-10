import Text from "@kit/Text";
import { useInterval } from "./hooks";
import { api } from "./api";
import { SlimCard } from "./ui/cards";
import { RelayLog } from "./ui/RelayLog";

/**
 * Inbound checks — live counters. Every inbound action runs the regex firewall
 * -> Ed25519 signature -> rationale check before the module sees it. P151
 * removed the NVIDIA-hosted semantic check: it sent text to a third party and
 * could not read sealed content anyway. Under them, the relay's transparency
 * log and this agent's own check of it (P281).
 */
export function TrustSafetySection() {
  const [stats] = useInterval(() => api.safetyStats(), 10_000);

  return (
    <SlimCard
      title="Trust & Safety"
      foot={
        "The regex firewall scans every incoming payload for prompt injection, on this relay, " +
        "with nothing sent elsewhere. Moves are signed by their players and games can be " +
        "checked independently (verify with kind=session). The rationale check is a nudge, not engine detection."
      }
    >
      <div className="e-kpi-row">
        <div className="e-kpi">
          <span className="e-kpi__val">{stats?.guardrails_mode ?? "—"}</span>
          <span className="e-kpi__lbl">guardrails</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{stats?.guardrail_blocks ?? 0}</span>
          <span className="e-kpi__lbl">regex blocks</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{stats?.por_rejections ?? 0}</span>
          <span className="e-kpi__lbl">rationale rejections</span>
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
      <Text className="e-dim" style={{ fontSize: "0.72rem", margin: "4px 0 8px" }}>
        Guardrails: with enforce, a message the firewall flags is refused; in any other mode it is let through.
      </Text>
      <RelayLog />
      {stats?.bot_fingerprints && stats.bot_fingerprints.length > 0 && (
        <Text className="e-dim" style={{ marginTop: 8, fontSize: "0.72rem" }}>
          Bot fingerprints watched: {stats.bot_fingerprints.join(", ")}
        </Text>
      )}
    </SlimCard>
  );
}
