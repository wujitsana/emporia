/**
 * P125: the dashboard's half of the local DM reader (`python -m emporia.dm_reader`).
 *
 * The relay stores DMs encrypted and holds no key; the dashboard holds none
 * either. The reader runs on the owner's machine, next to the agents' keys, and
 * opens sealed messages for the agent being viewed. Its token arrives once in
 * the link it prints (`?reader=…&reader_port=…`), is kept for this browser
 * session only, and is removed from the address bar so it is not bookmarked or
 * shared. Without a reader everything still works — sealed messages show 🔒.
 */
import type { DMMessage } from "./api";

const KEY = "emporia_dm_reader";

export interface ReaderConfig { token: string; port: number }

export function readerConfigFromUrl(search: string): ReaderConfig | null {
  const q = new URLSearchParams(search);
  const token = q.get("reader")?.trim() ?? "";
  const port = Number(q.get("reader_port") || 8089);
  return /^[A-Za-z0-9_-]{16,}$/.test(token) && port > 0 && port < 65536 ? { token, port } : null;
}

export function readerConfig(): ReaderConfig | null {
  if (typeof window === "undefined") return null;
  const fromUrl = readerConfigFromUrl(window.location.search);
  try {
    if (fromUrl) {
      sessionStorage.setItem(KEY, JSON.stringify(fromUrl));
      const url = new URL(window.location.href);
      url.searchParams.delete("reader");
      url.searchParams.delete("reader_port");
      window.history.replaceState(null, "", url.toString());
      return fromUrl;
    }
    const saved = sessionStorage.getItem(KEY);
    return saved ? (JSON.parse(saved) as ReaderConfig) : null;
  } catch {
    return fromUrl;
  }
}

/** `{message_id: text}` for the sealed messages the reader could open. */
export async function openSealedDms(
  cfg: ReaderConfig, viewer: string, threadId: string, messages: DMMessage[],
): Promise<Record<string, string>> {
  const r = await fetch(`http://127.0.0.1:${cfg.port}/dm/open`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-emporia-reader-token": cfg.token },
    body: JSON.stringify({ viewer, thread_id: threadId, messages }),
  });
  if (!r.ok) throw new Error(`reader ${r.status}`);
  const d = (await r.json()) as { opened?: Record<string, string | null> };
  return Object.fromEntries(Object.entries(d.opened ?? {}).filter(
    (e): e is [string, string] => typeof e[1] === "string"));
}
