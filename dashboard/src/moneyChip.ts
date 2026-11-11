/**
 * The header's money chip (review pass 2, P6): beside the relay chip, so every frame that
 * shows an amount also says what kind of money it is.
 *
 * From `/health`: the relay's own `live_money` (what makes it move real money, if anything),
 * where it holds USDC escrow (`x402_escrow_network`), the x402 networks it takes, and where
 * Tempo entries are paid. Only a known test network
 * counts as test money (networks.py); anything else is "real money", said plainly, with no
 * promise attached. A relay that names no money rail at all gets no chip: nothing it shows
 * is money.
 */
import type { Health } from "./api";
import { isKnownTestnet, networkWithKind } from "./paymentFormat";

export interface MoneyChip {
  kind: "test" | "real";
  label: string;
  title: string;
}

export function moneyChip(h: Health | null | undefined): MoneyChip | null {
  if (!h || h.status !== "ok") return null;
  const nets = [
    h.x402_escrow_network,
    h.tempo_network,
    ...(h.x402_enabled ? h.x402_networks ?? [] : []),
  ].filter((n): n is string => typeof n === "string" && n.length > 0);
  const unique = [...new Set(nets)];
  const real = [
    ...(h.live_money ?? []),
    ...unique.filter((n) => !isKnownTestnet(n)).map((n) => `USDC on ${networkWithKind(n)}`),
  ];
  if (real.length) {
    return {
      kind: "real",
      label: "real money",
      title: `This relay moves real money (${[...new Set(real)].join(", ")}). Amounts shown here are real.`,
    };
  }
  const where = [
    ...unique.map(networkWithKind),
  ];
  if (!where.length) return null;
  return {
    kind: "test",
    label: "testnet",
    title: `Test money only: amounts here are on ${where.join(", ")}, with no real value.`,
  };
}
