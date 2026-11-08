/** A fee in words: "free", "$5.00", or "$5.00 EUR" for a currency other than USD. */
export function feeLabel(cents: number | undefined | null, currency?: string | null): string {
  if (!cents || cents <= 0) return "free";
  const amount = `$${(cents / 100).toFixed(2)}`;
  const cur = (currency ?? "").toUpperCase();
  return cur && cur !== "USD" ? `${amount} ${cur}` : amount;
}
