/**
 * P105: how money states read on the dashboard.
 *
 * The relay grew states the UI never learned: a settlement whose capture
 * failed is still written `status: "settled"` (its `transfer_status` says
 * `capture_failed`), x402 escrow holds and destination charges settle with no
 * transfer at all, and bounties gained `awaiting_payment`, `defaulted`,
 * `unfunded` and `cancelling`. Each fell through to the generic rendering — a
 * failed capture showed the same ● as money delivered. Same failure the
 * BountyBoard comment names: the reassuring reading, chosen by omission.
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
  if (FAILED_TRANSFERS.has(t) || t.startsWith("capture_failed")) return "failed";
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

export function networkLabel(network: string): string {
  if (network === "eip155:8453") return "Base";
  if (network === "eip155:84532") return "Base Sepolia";
  return network;
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

/** A bounty state, for the board: a short label and what it means. */
export function bountyStateLabel(
  state: string,
  full: boolean,
): { label: string; title: string; failed?: boolean } {
  switch (state) {
    case "open":
      return full
        ? { label: "ready", title: "Seats are full; waiting to be started." }
        : { label: "open", title: "Open for entry." };
    case "running":
      return { label: "running", title: "Being played now." };
    case "awaiting_payment":
      return { label: "awaiting payment", title: "A winner is named; the funder owes the promised prize." };
    case "defaulted":
      return { label: "defaulted", title: "The funder did not pay the promised prize in time.", failed: true };
    case "unfunded":
      return { label: "unfunded", title: "The held prize could not be captured, so the winner was not paid.", failed: true };
    case "settled":
      return { label: "paid", title: "The prize was paid to the winner." };
    case "refunded":
      return { label: "refunded", title: "No winner; the prize went back to the funder." };
    case "cancelling":
      return { label: "cancelling", title: "Being withdrawn; the hold is being released." };
    case "cancelled":
      return { label: "cancelled", title: "Withdrawn by the funder before anyone entered." };
    default:
      return { label: state, title: `This bounty is ${state}.` };
  }
}

/** A session status, for the session lists. */
export function sessionStatusLabel(status: string): string {
  if (status === "awaiting_funding") return "awaiting funding";
  if (status === "funding") return "funding…";
  return status;
}
