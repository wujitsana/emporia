/**
 * P295: the Hermes tab's doc texts, fetched from the console (`GET /docs/<path>`),
 * which reads them from the Emporia it runs. Bundled, a whole doc is one line of
 * JavaScript, and the catalog's install scan reads commands in it across what were
 * separate lines — `rm` in one paragraph and `~` in another as one command.
 */
import LIST from "./docList.json";
import { consoleUrl, httpFetch } from "./transport";

export const BUNDLED_TEXTS: Record<string, string> | null = null;

export async function fetchDocTexts(): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  await Promise.all(LIST.map(async (d) => {
    const r = await httpFetch(consoleUrl(`/docs/${d.path}`)).catch(() => null);
    out[d.path] = r && r.ok ? await r.text() : `This document did not load (${r ? r.status : "no answer"}).`;
  }));
  return out;
}
