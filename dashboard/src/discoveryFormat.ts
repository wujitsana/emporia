/**
 * Pure display helpers for the discovery view (listings + lobby challenges).
 *
 * These live outside ListingsView.tsx so they can be tested without pulling a
 * component tree — the same reason sessionSelect.ts exists.
 */

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
