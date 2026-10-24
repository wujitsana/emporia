import Text from "@components/Text";
import { useInterval } from "../hooks";
import { api } from "../api";
import type { Session } from "../api";
import { AcceptsTag, PaidByTag } from "./PayTag";

/**
 * P153: a session's money in one line — its stake, then what can pay it
 * (before anyone has) or how it was paid (after). Nothing for a free session.
 */
export function SessionMoney({ session }: { session: Session }) {
  const [full] = useInterval(() => api.session(session.session_id), 30_000, [session.session_id]);
  const rules = full?.payment_rules ?? session.payment_rules;
  const stake = Number(rules?.stake_per_participant ?? 0);
  if (!stake) return null;
  const purchase = session.module_type === "emporia:service:v1";
  const paid = full?.paid_by ?? [];
  return (
    <Text className="e-dim" style={{ fontSize: 11 }}>
      {purchase ? "price" : "stake"} {stake.toFixed(2)} {(rules?.currency ?? "").toUpperCase()} ·{" "}
      {paid.length ? <PaidByTag rails={paid} /> : <AcceptsTag move={purchase ? "purchase" : "wager"} />}
    </Text>
  );
}
