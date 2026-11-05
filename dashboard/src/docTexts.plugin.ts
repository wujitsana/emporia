/**
 * P295: the doc texts of the Hermes tab and the Desktop page, fetched from the
 * console (`GET /doc-texts`, every listed doc in one JSON answer), which reads
 * them from the Emporia it runs. JSON because the Desktop page's only door is
 * `ctx.rest`, which carries JSON alone. Not bundled: a whole doc is one line of
 * JavaScript, and the catalog's install scan reads commands in it across what
 * were separate lines — `rm` in one paragraph and `~` in another as one command.
 */
import LIST from "./docList.json";
import { consoleUrl, httpFetch } from "./transport";

export const BUNDLED_TEXTS: Record<string, string> | null = null;

export async function fetchDocTexts(): Promise<Record<string, string>> {
  const r = await httpFetch(consoleUrl("/doc-texts")).catch(() => null);
  const texts: Record<string, unknown> = r && r.ok
    ? ((await r.json().catch(() => ({}))) as { texts?: Record<string, unknown> }).texts ?? {}
    : {};
  const why = `This document did not load (${r ? r.status : "no answer"}).`;
  return Object.fromEntries(LIST.map((d) => {
    const t = texts[d.path];
    return [d.path, typeof t === "string" ? t : why];
  }));
}
