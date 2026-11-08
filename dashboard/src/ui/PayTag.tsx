import { useRelayHealth } from "../healthCtx";
import { acceptsLabel } from "../paymentFormat";
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
