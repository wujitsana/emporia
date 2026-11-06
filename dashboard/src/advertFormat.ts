/**
 * P249: rooms and Agora topics that linked relays advertised, as rows the
 * "On other relays" section shows.
 *
 * Pure, so it can be tested without a component tree. The dashboard only reads
 * these: joining and paying happen on the relay that holds the room or topic,
 * which is why every row names it.
 */
export interface ElsewhereRow {
  key: string;
  name: string;
  /** The relay that holds it — where it is joined and paid for. */
  host: string;
  owner: string;
  fingerprint: string;
  /** "free" or "$5.00". */
  fee: string;
  note: string;
}

/** A fee in words: "free", "$5.00", or "$5.00 EUR" for a currency other than USD. */
export function feeLabel(cents: number | undefined | null, currency?: string | null): string {
  if (!cents || cents <= 0) return "free";
  const amount = `$${(cents / 100).toFixed(2)}`;
  const cur = (currency ?? "").toUpperCase();
  return cur && cur !== "USD" ? `${amount} ${cur}` : amount;
}
