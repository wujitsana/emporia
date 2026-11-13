import { looksLikeEncryptedEnvelope } from "./api";

/** One line of a room message for a card or a list: the words of a v2 post body, never its JSON.
 *  Used wherever a room message is previewed (Home room card, lab room block); the room view itself
 *  reads the same bodies through `postParts`. Untrusted text, shown as data. */
export const PREVIEW_REMOVED = "removed";
export const PREVIEW_SEALED = "sealed";
export const PREVIEW_OTHER = "message";

const oneLine = (s: string): string => s.replace(/\s+/g, " ").trim();

export function roomMessagePreview(content: string | null | undefined, opts: { removed?: boolean } = {}): string {
  if (opts.removed) return PREVIEW_REMOVED;
  const raw = (content ?? "").trim();
  if (!raw) return "";
  if (looksLikeEncryptedEnvelope(raw)) return PREVIEW_SEALED;
  if (raw[0] !== "{") return oneLine(raw);
  let body: Record<string, unknown> | null = null;
  try {
    const b = JSON.parse(raw) as unknown;
    if (b && typeof b === "object" && !Array.isArray(b)) body = b as Record<string, unknown>;
  } catch {
    // A body cut short by a relay's first-line clip: the words may still be readable.
    const m = /"text"\s*:\s*("(?:[^"\\]|\\.)*")/.exec(raw);
    if (m) {
      try { body = { text: JSON.parse(m[1]) as string }; } catch { /* fall through */ }
    }
  }
  if (!body) return PREVIEW_OTHER;
  const kind = typeof body.kind === "string" && body.kind !== "note"
    ? (body.kind === "other" && typeof body.label === "string" && body.label ? body.label : body.kind) : "";
  const text = typeof body.text === "string" ? oneLine(body.text) : "";
  if (text) return kind ? `[${kind}] ${text}` : text;
  return kind ? `[${kind}]` : PREVIEW_OTHER;
}
