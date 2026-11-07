/**
 * P105: how money states read on the dashboard.
 *
 * The relay grew states the UI never learned: a settlement whose capture
 * failed is still written `status: "settled"` (its `transfer_status` says
 * `capture_failed`), x402 escrow holds and destination charges settle with no
 * transfer at all. Each fell through to the generic rendering — a
 * failed capture showed the same ● as money delivered. The reassuring reading, chosen by omission.
 */
import type { Settlement } from "./api";

export type SettlementHealth = "settled" | "pending" | "failed" | "refunded";

const FAILED_TRANSFERS = new Set(["capture_failed", "transfer_failed"]);
// The money reached the payee without a relay transfer: a direct x402 payment,
// a captured escrow hold, a Stripe destination charge.
const DIRECT_TRANSFERS = new Set(["direct", "escrow_captured", "destination", "transferred"]);

export function settlementHealth(s: Pick<Settlement, "status" | "transfer_status">): SettlementHealth {
  if (s.status === "refunded") return "refunded";
  const t = s.transfer_status ?? "";
  // A session payout that raised is written `transfer_failed: <reason>` (payments.settle).
  if (FAILED_TRANSFERS.has(t) || t.startsWith("capture_failed") || t.startsWith("transfer_failed")) return "failed";
  if (DIRECT_TRANSFERS.has(t) || t === "no_payment") return "settled";
  // pending_connect*, no_stripe_account: owed, not yet paid out.
  return t ? "pending" : "settled";
}

export function settlementHealthTitle(s: Pick<Settlement, "status" | "transfer_status">): string {
  switch (settlementHealth(s)) {
    case "failed":
      return "The payment could not be captured, so nothing was paid out.";
    case "pending":
      return `Owed to the payee, not yet paid out (${s.transfer_status}).`;
    case "refunded":
      return "Returned to the payer.";
    default:
      return "Paid to the payee.";
  }
}

/** The settlement rail as a reader thinks of it. */
export function railLabel(rail: string | undefined, transferStatus?: string): string {
  if (!rail) return "—";
  if (rail === "x402") return transferStatus === "escrow_captured" ? "x402 escrow" : "x402";
  if (rail === "stripe") return transferStatus === "destination" ? "card (direct)" : "card";
  return rail;
}

const EXPLORERS: Record<string, string> = {
  "eip155:8453": "https://basescan.org",
  "eip155:84532": "https://sepolia.basescan.org",
};

/** A block-explorer link for a transaction, or null on a network without one. */
export function explorerTxUrl(network: string, tx: string | null | undefined): string | null {
  const base = EXPLORERS[network];
  return base && tx ? `${base}/tx/${tx}` : null;
}

/** P160: the same names as src/emporia/networks.py. */
const NETWORK_NAMES: Record<string, string> = {
  "eip155:8453": "Base",
  "eip155:84532": "Base Sepolia",
  "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp": "Solana",
  "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1": "Solana devnet",
  "eip155:4217": "Tempo",
  "eip155:42431": "Tempo Moderato",
};

export function networkLabel(network: string): string {
  return NETWORK_NAMES[network] ?? network;
}

/** Micro-dollars (= USDC atomic units, 6 decimals) as dollars. Whole cents as
 *  cents; a sub-cent amount (a fee split) keeps its digits rather than
 *  rounding — $0.975 is what the seller received, not $0.98. */
export function formatMicros(n: number): string {
  if (!Number.isFinite(n)) return "—";
  return n % 10_000 === 0 ? `$${(n / 1e6).toFixed(2)}` : `$${(n / 1e6).toFixed(6).replace(/0+$/, "")}`;
}

/** USDC atomic units (6 decimals) as dollars. */
export function usdcAtomicToUsd(amount: string): string {
  const n = Number(amount);
  return Number.isFinite(n) ? formatMicros(n) : amount;
}

type SplitFields = Pick<Settlement, "platform_fee_cents" | "winner_payout_cents" |
  "platform_fee_atomic" | "winner_payout_atomic">;

function micros(atomic: string | null | undefined, cents: number): number {
  const n = atomic == null ? NaN : Number(atomic);
  return Number.isFinite(n) ? n : cents * 10_000;
}

/** P129: a settlement's fee in micro-dollars — the contract's exact split for
 *  an escrow capture, the cents column everywhere else. Sum these, then
 *  formatMicros(), so totals agree with what the chain paid. */
export function settlementFeeMicros(s: SplitFields): number {
  return micros(s.platform_fee_atomic, s.platform_fee_cents);
}

export function settlementPayoutMicros(s: SplitFields): number {
  return micros(s.winner_payout_atomic, s.winner_payout_cents);
}

/** P111: a hold the seller may not be paid from — shown as a warning. */
export function holdNeedsAttention(state: string): boolean {
  return state === "capture_pending" || state === "expired";
}

export function holdStateTitle(state: string): string {
  switch (state) {
    case "authorized":
      return "Held in the escrow contract — captured to the seller on confirm, voided back on dispute.";
    case "captured":
      return "Captured: the seller was paid, less the fee.";
    case "voided":
      return "Voided: returned to the buyer.";
    case "reclaimed":
      return "Reclaimed by the buyer after the hold expired.";
    case "capture_pending":
      return "Capturing to the seller failed once; the relay retries until the hold expires.";
    case "void_pending":
      return "Returning it to the buyer failed once; the relay retries.";
    case "expired":
      return "The hold expired before it could be captured; the buyer can reclaim it.";
    default:
      return state;
  }
}

/** A session status, for the session lists. */
export function sessionStatusLabel(status: string): string {
  if (status === "awaiting_funding") return "awaiting funding";
  if (status === "funding") return "funding…";
  return status;
}

/** P153: what can pay for something, before anyone does. */
export type MoneyMove = "entry" | "purchase";

export function acceptsLabel(
  move: MoneyMove,
  h: { stripe_enabled?: boolean; x402_enabled?: boolean; x402_networks?: string[];
       x402_escrow_network?: string | null; tempo_network?: string | null;
       custodial_cards?: boolean } | null,
): string {
  const card = !!h?.stripe_enabled;
  const usdc = !!h?.x402_enabled;
  // P160: name the networks, so a reader knows which wallet and which chain.
  const on = (nets: (string | null | undefined)[]) => {
    const names = nets.filter((n): n is string => !!n).map(networkWithKind);
    return names.length ? ` (${names.join(", ")})` : "";
  };
  const ways: string[] = [];
  switch (move) {
    case "entry":            // a room or an Agora: card, or stablecoin straight to its creator
      if (card) ways.push("card");
      if (usdc) ways.push(`USDC${on(h?.x402_networks ?? [])}`);
      if (h?.tempo_network) ways.push(`Tempo${on([h.tempo_network])}`);
      break;
    case "purchase":         // card held for the seller, or USDC held in escrow
      if (card) ways.push("card");
      if (h?.x402_escrow_network) ways.push(`USDC escrow${on([h.x402_escrow_network])}`);
      break;
  }
  return ways.length ? `accepts ${ways.join(" · ")}` : "no payment rail on this relay";
}

/** The test networks (src/emporia/networks.py marks the same ones). */
const TESTNETS = new Set(["eip155:84532", "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1", "eip155:42431"]);

/**
 * Whether payments on this escrow network are test money. A relay that does not
 * name its escrow network is holding test funds: agents pay on testnets unless
 * a person unlocks a real-money network for that agent in their own terminal.
 */
export function isTestnet(network: string | null | undefined): boolean {
  return !network || TESTNETS.has(network);
}

/** Whether this network is one of the known test networks. Unlike `isTestnet`, an unnamed
 *  or unknown network is not: only a known testnet counts as test money (networks.py). */
export function isKnownTestnet(network: string | null | undefined): boolean {
  return !!network && TESTNETS.has(network);
}

/** "Base Sepolia testnet", "Solana devnet": a network's name that says when it is a test one. */
export function networkWithKind(network: string): string {
  const name = networkLabel(network);
  return TESTNETS.has(network) && !/devnet|testnet/i.test(name) ? `${name} testnet` : name;
}

/**
 * The money line under a deal, in two parts: what is paid
 * ("1.00 USDC (testnet)") and how it is held ("escrow", "card"), or null when
 * nobody has paid yet. USDC on a session is always held in escrow.
 */
export function priceText(amount: number, currency: string | undefined, opts: {
  rails?: string[]; mode?: string; escrowNetwork?: string | null;
}): { amount: string; via: string | null } {
  const rails = opts.rails ?? [];
  const usdc = rails.includes("x402") || opts.mode === "x402";
  const unit = usdc ? `USDC${isTestnet(opts.escrowNetwork) ? " (testnet)" : ""}` : (currency ?? "").toUpperCase();
  const via = rails.length
    ? rails.map((r) => (r === "x402" ? "escrow" : r === "stripe" ? "card" : r === "tempo" ? "Tempo" : r)).join(" + ")
    : null;
  return { amount: `${amount.toFixed(2)} ${unit}`.trim(), via };
}
