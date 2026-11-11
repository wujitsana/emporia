import Text from "@kit/Text";
import type { Settlement } from "./api";
import { useRelayHealth } from "./healthCtx";
import { SlimCard } from "./ui/cards";
import { SessionLink } from "./ui/chips";
import type { Navigate } from "./navigation";
import {
  formatMicros, settlementFeeMicros, settlementHealth, settlementHealthTitle, settlementPayoutMicros,
} from "./paymentFormat";

/* P43-2: this took health as a `seed` prop and polled /health anyway, and
   polled /payments/settlements on a timer identical to the one the Overview
   was already running. Both facts now arrive from the single fetch above it —
   health through the context, settlements as a prop. */
export function PaymentsFeesSection({ settlements, navigate }: { settlements: Settlement[]; navigate?: Navigate }) {
  const health = useRelayHealth();
  // P105: a failed capture moved no money — counted apart, never summed.
  const moved = settlements.filter((s) => settlementHealth(s) !== "failed");
  const failedCount = settlements.length - moved.length;
  const totalMovedCents = moved.reduce((s, r) => s + r.total_price_cents, 0);
  const totalFeeMicros = moved.reduce((s, r) => s + settlementFeeMicros(r), 0);
  const feeBps = health?.fee_bps ?? 0;
  const feeLabel = `${(feeBps / 100).toFixed(1)}%`;
  const rails = health?.payment_rails?.join(", ") || "none";
  const cap = health?.max_total_spend_cents;

  return (
    <SlimCard
      title="Payments"
      foot={`x402 (USDC) and Tempo. Total spend limit: ${cap && cap > 0 ? `$${(cap / 100).toFixed(2)}` : "unlimited"}` }
    >
      <div className="e-kpi-row">
        <div className="e-kpi">
          <span className="e-kpi__val">{feeLabel}</span>
          <span className="e-kpi__lbl">fee</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{rails}</span>
          <span className="e-kpi__lbl">rails</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{moved.length}</span>
          <span className="e-kpi__lbl">settled</span>
        </div>
        {failedCount > 0 && (
          <div className="e-kpi" title="Captures that failed: nothing was paid out on these.">
            <span className="e-kpi__val" style={{ color: "var(--theme-warning, #c33)" }}>{failedCount}</span>
            <span className="e-kpi__lbl">failed</span>
          </div>
        )}
        <div className="e-kpi">
          <span className="e-kpi__val">${(totalMovedCents / 100).toFixed(2)}</span>
          <span className="e-kpi__lbl">volume</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{formatMicros(totalFeeMicros)}</span>
          <span className="e-kpi__lbl">fees</span>
        </div>
      </div>
      {settlements.length > 0 ? (
        <ul className="e-settlement-list">
          {settlements.slice(0, 5).map((s) => (
            <li key={s.session_id} className="e-settlement-list__item">
              <SessionLink session_id={s.session_id} navigate={navigate} />
              <Text>
                {s.status === "refunded"
                  ? "refund"
                  : settlementHealth(s) === "failed"
                    ? "not paid"
                    : formatMicros(settlementPayoutMicros(s))}
              </Text>
              <Text
                className="e-faint"
                title={settlementHealthTitle(s)}
                style={settlementHealth(s) === "failed" ? { color: "var(--theme-warning, #c33)" } : undefined}
              >
                {settlementHealth(s)}
              </Text>
            </li>
          ))}
        </ul>
      ) : (
        <Text className="e-dim" style={{ marginTop: 8, fontSize: "0.72rem" }}>
          No settlements recorded yet — appears when deals and paid entries settle.
        </Text>
      )}
    </SlimCard>
  );
}