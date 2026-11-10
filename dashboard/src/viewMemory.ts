/**
 * What the person last chose in each view (sub-tab per destination, Feed and Topics filters, the
 * Arena's page), kept in sessionStorage for this browser tab only. Storage can be missing or throw
 * (private window, blocked site data): every read and write is guarded and the view works without it.
 */
const PREFIX = "emporia.view.";

export function recall(key: string): string | null {
  try {
    return window.sessionStorage.getItem(PREFIX + key);
  } catch {
    return null;
  }
}

export function remember(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(PREFIX + key, value);
  } catch {
    /* no storage: nothing is kept */
  }
}

/** The remembered value if it is one of the allowed ones, else the fallback. */
export function recallOf<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  const v = recall(key);
  return (allowed as readonly string[]).includes(v ?? "") ? (v as T) : fallback;
}
