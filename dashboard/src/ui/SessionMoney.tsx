import Text from "@kit/Text";
import { useInterval } from "../hooks";
import { api } from "../api";
import type { Session } from "../api";
import { useRelayHealth } from "../healthCtx";
import { stakeText } from "../paymentFormat";
import { AcceptsTag } from "./PayTag";

/**
 * P153: a session's money in one line — its stake, then what can pay it
 * (before anyone has) or how it is held (after): "stake 1.00 USDC (testnet) ·
 * escrow". Nothing for a free session.
 */
export function SessionMoney({ session }: { session: Session }) {
  const [full] = useInterval(() => api.session(session.session_id), 30_000, [session.session_id]);
  const health = useRelayHealth();
  const rules = full?.payment_rules ?? session.payment_rules;
  const stake = Number(rules?.stake_per_participant ?? 0);
  if (!stake) return null;
  const purchase = session.module_type === "emporia:service:v1";
  const money = stakeText(stake, rules?.currency, {
    rails: full?.paid_by ?? [], mode: rules?.mode, escrowNetwork: health?.x402_escrow_network,
  });
  return (
    <Text className="e-dim" style={{ fontSize: 11 }}>
      {purchase ? "price" : "stake"} {money.amount} ·{" "}
      {money.via ?? <AcceptsTag move={purchase ? "purchase" : "wager"} moduleType={session.module_type} />}
    </Text>
  );
}
