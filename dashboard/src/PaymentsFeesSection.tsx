import Text from "@components/Text";
import { useInterval } from "./hooks";
import { api } from "./api";
import type { RelayHealth } from "./hooks";
import { SlimCard } from "./ui/cards";

export function PaymentsFeesSection({ seed }: { seed: RelayHealth | null }) {
  const [polled] = useInterval(() => api.health(), 10_000);
  const [settlementsData] = useInterval(() => api.settlements(), 15_000);
  const health = polled ?? seed;
  const settlements = settlementsData?.settlements ?? [];
  const totalStakeCents = settlements.reduce((s, r) => s + r.total_stake_cents, 0);
  const totalFeeCents = settlements.reduce((s, r) => s + r.platform_fee_cents, 0);
  const feeBps = health?.operator_fee_bps ?? 250;
  const feeLabel = `${(feeBps / 100).toFixed(1)}%`;
  const methods = health?.payment_methods?.join(", ") || "none";
  const cap = health?.max_total_spend_cents;
  const stripeState = health?.stripe_enabled
    ? health?.stripe_profile_ready
      ? "ready"
      : "key only"
    : "off";

  return (
    <SlimCard
      title="Payments"
      foot={`Relay-settled MPP commerce. Total spend limit: ${cap && cap > 0 ? `$${(cap / 100).toFixed(2)}` : "unlimited"}` }
    >
      <div className="e-kpi-row">
        <div className="e-kpi">
          <span className="e-kpi__val">{feeLabel}</span>
          <span className="e-kpi__lbl">fee</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{health?.mpp_enabled ? "on" : "off"}</span>
          <span className="e-kpi__lbl">mpp</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{methods}</span>
          <span className="e-kpi__lbl">methods</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{stripeState}</span>
          <span className="e-kpi__lbl">stripe profile</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{settlements.length}</span>
          <span className="e-kpi__lbl">settled</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">${(totalStakeCents / 100).toFixed(0)}</span>
          <span className="e-kpi__lbl">volume</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">${(totalFeeCents / 100).toFixed(2)}</span>
          <span className="e-kpi__lbl">fees</span>
        </div>
      </div>
      {health?.stripe_mpp_admin_notice ? (
        <Text className="e-dim" style={{ marginTop: 8, fontSize: "0.72rem", color: "var(--e-warn, #c9a227)" }}>
          {health.stripe_mpp_admin_notice}
        </Text>
      ) : null}
      {settlements.length > 0 ? (
        <ul className="e-settlement-list">
          {settlements.slice(0, 5).map((s) => (
            <li key={s.session_id} className="e-settlement-list__item">
              <Text className="e-dim">…{s.session_id.slice(-8)}</Text>
              <Text>
                {s.status === "refunded"
                  ? "refund"
                  : `$${(s.winner_payout_cents / 100).toFixed(2)}`}
              </Text>
              <Text className="e-faint">{s.status}</Text>
            </li>
          ))}
        </ul>
      ) : (
        <Text className="e-dim" style={{ marginTop: 8, fontSize: "0.72rem" }}>
          No settlements recorded yet — appears when staked games complete.
        </Text>
      )}
    </SlimCard>
  );
}