import Text from "@components/Text";
import { useInterval } from "./hooks";
import { api } from "./api";
import { SlimCard } from "./ui/cards";

/**
 * Guardrails + Proof-of-Reasoning anti-cheat — live counters.
 * Every inbound action runs guardrails -> Ed25519 signature -> PoR before the
 * module ever sees it; this panel makes that pipeline visible instead of
 * leaving it as a server-side implementation detail. Two guardrail layers are
 * shown separately: the always-on regex firewall, and the optional NVIDIA
 * NIM-backed semantic check (an actual Nemotron model call, not a pattern match).
 */
export function TrustSafetySection() {
  const [stats] = useInterval(() => api.safetyStats(), 10_000);
  const nemoOn = stats?.nemo_guardrails_enabled ?? false;

  return (
    <SlimCard
      title="Trust & Safety"
      action={
        <span className={`e-status-pill ${nemoOn ? "e-status-pill--on" : "e-status-pill--off"}`}>
          {nemoOn ? `NIM: ${stats?.nemo_guardrails_model}` : "NIM: off"}
        </span>
      }
      foot={
        nemoOn
          ? `Regex firewall (always on) + NVIDIA NIM semantic check via ${stats?.nemo_guardrails_model} for paraphrased/novel injection attempts the regex layer can't generalize past.`
          : "Regex firewall scans every payload for prompt injection. NeMo NIM is enabled on the relay but inactive without NVIDIA_API_KEY — restart the relay after install/bootstrap."
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
          <span className="e-kpi__val">{stats?.nemo_guardrail_blocks ?? 0}</span>
          <span className="e-kpi__lbl">NIM blocks</span>
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
