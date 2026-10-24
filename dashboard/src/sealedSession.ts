import { looksLikeEncryptedEnvelope } from "./api";
import { consoleUrl, httpFetch, VIA_CONSOLE } from "./transport";
import { VIEWER_ID } from "./relayEnv";

export interface SealedItem {
  id: string;
  field: string;
  content: string;
}

/** P149: the sealed fields of a private session's payloads, one item each. */
export function sealedItems(rows: { id: string; payload: Record<string, unknown> | null | undefined }[]): SealedItem[] {
  const out: SealedItem[] = [];
  for (const r of rows) {
    for (const [field, v] of Object.entries(r.payload ?? {})) {
      if (typeof v === "string" && looksLikeEncryptedEnvelope(v)) out.push({ id: `${r.id}:${field}`, field, content: v });
    }
  }
  return out;
}

/**
 * P149: a private session's sealed text, opened by the local console with the
 * viewer's own key, on this machine. Without the console (the dashboard served
 * by the relay) there is no key, and it stays 🔒.
 */
export async function openSealed(sessionId: string, items: SealedItem[]): Promise<Record<string, string>> {
  if (!VIA_CONSOLE || items.length === 0 || !VIEWER_ID) return {};
  const r = await httpFetch(
    consoleUrl(`/session/${encodeURIComponent(VIEWER_ID)}/${encodeURIComponent(sessionId)}/open`),
    { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ items }) });
  if (!r.ok) return {};
  const d = (await r.json()) as { opened?: Record<string, string | null> };
  return Object.fromEntries(Object.entries(d.opened ?? {}).filter(
    (e): e is [string, string] => typeof e[1] === "string"));
}
