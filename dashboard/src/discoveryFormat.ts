/**
 * Pure display helpers for the discovery view (listings + lobby challenges).
 *
 * These live outside ListingsView.tsx so they can be tested without pulling a
 * component tree — the same reason sessionSelect.ts exists.
 */

/**
 * Host of the relay a challenge came from, or "local" when it originated here.
 *
 * P40-1: `new URL()` throws on anything that isn't a valid absolute URL, and
 * this value arrives from a federated peer — i.e. from outside this relay's
 * control. A throw during render takes down the whole view, not just the cell,
 * so an unparseable origin degrades to a label instead.
 */
export function relayHost(originRelay?: string | null): string {
  if (!originRelay) return "local";
  try {
    return new URL(originRelay).host || originRelay;
  } catch {
    return originRelay.slice(0, 24);
  }
}

/** Compact time-to-expiry: "3h", "5d", or "expired". */
export function expiryLabel(expiresAt?: string | null, now: number = Date.now()): string {
  if (!expiresAt) return "";
  const ms = new Date(expiresAt).getTime() - now;
  if (Number.isNaN(ms)) return "";
  if (ms < 0) return "expired";
  const h = Math.floor(ms / 3_600_000);
  if (h < 48) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}
