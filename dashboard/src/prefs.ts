/**
 * Small preferences the dashboard keeps on this machine: the theme, the tint,
 * the room-chain pins (integrityCheck.ts). In a browser they live in
 * localStorage; on the Hermes Desktop page in `ctx.storage`, namespaced to this
 * plugin, so the page never writes into the app's own storage keys. Strings in,
 * strings out; every failure reads as "not set".
 */
import { MODE } from "./transport";
import { desktopBridge } from "./desktop/bridge";

function desktopStorage() {
  return MODE === "desktop" ? desktopBridge()?.storage ?? null : null;
}

export function readPref(key: string): string | null {
  const store = desktopStorage();
  try {
    if (store) {
      const v = store.get<unknown>(key, null);
      return typeof v === "string" ? v : null;
    }
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writePref(key: string, value: string | null): void {
  const store = desktopStorage();
  try {
    if (store) {
      if (value === null) store.remove(key);
      else store.set(key, value);
    } else if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* best effort: a preference that is not kept is only a preference */
  }
}
