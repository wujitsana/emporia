import { useRelayHealth } from "../healthCtx";
import { acceptsLabel, paidByLabel } from "../paymentFormat";
import type { MoneyMove } from "../paymentFormat";

/** P153: what can pay for this, shown beside its price before anyone pays. */
export function AcceptsTag({ move }: { move: MoneyMove }) {
  const health = useRelayHealth();
  const label = acceptsLabel(move, health);
  return (
    <span className="e-faint" style={{ fontSize: 11 }} title={label}>
      {label.replace(/^accepts /, "")}
    </span>
  );
}

/** P153: how a session's stakes were actually paid. Renders nothing if unpaid. */
export function PaidByTag({ rails }: { rails?: string[] }) {
  const label = paidByLabel(rails);
  return label ? <span className="e-faint" style={{ fontSize: 11 }}>{label}</span> : null;
}
