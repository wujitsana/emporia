import Text from "@kit/Text";
import { useInterval } from "../hooks";
import { api } from "../api";
import type { Session } from "../api";
import { useRelayHealth } from "../healthCtx";
import { priceText } from "../paymentFormat";
import { AcceptsTag } from "./PayTag";

/**
 * P153: a purchase's money in one line — its price, then what can pay it
 * (before anyone has) or how it is held (after): "price 1.00 USDC (testnet) ·
 * escrow". Nothing for a game: games carry no stakes.
 */
export function SessionMoney({ session }: { session: Session }) {
  const [full] = useInterval(() => api.session(session.session_id), 30_000, [session.session_id]);
  const health = useRelayHealth();
  const rules = full?.payment_rules ?? session.payment_rules;
  const price = Number(rules?.price ?? 0);
  if (!price || session.module_type !== "emporia:service:v1") return null;
  const money = priceText(price, rules?.currency, {
    rails: full?.paid_by ?? [], mode: rules?.mode, escrowNetwork: health?.x402_escrow_network,
  });
  return (
    <Text className="e-dim" style={{ fontSize: 11 }}>
      price {money.amount} ·{" "}
      {money.via ?? <AcceptsTag move="purchase" />}
    </Text>
  );
}
