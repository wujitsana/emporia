/**
 * P171: the documents the dashboard carries, in reading order.
 *
 * The list is docList.json (the console reads it too). The texts are bundled
 * when the console and relay builds are made (docTexts.ts), so they match the
 * code and work offline; the Hermes tab fetches them from its console (P295,
 * docTexts.plugin.ts). The long history files (the security log, the archive)
 * stay in the repository.
 */

import LIST from "./docList.json";
import { BUNDLED_TEXTS, fetchDocTexts } from "./docTexts";
import type { LinkTarget } from "./markdown";

export interface Doc {
  id: string;
  title: string;
  blurb: string;
  /** Path in the repository, for "where is this" and for resolving links. */
  path: string;
  text: string;
}

export const DOCS: Doc[] = LIST.map((d) => ({ ...d, text: BUNDLED_TEXTS?.[d.path] ?? "" }));

let loading: Promise<void> | null = null;

/** Resolves once every doc has its text (at once when bundled). Fetched on the
 *  first call, not at import: the Desktop page has no transport until it registers. */
export function docsReady(): Promise<void> {
  loading ??= BUNDLED_TEXTS
    ? Promise.resolve()
    : fetchDocTexts().then((texts) => { for (const d of DOCS) d.text = texts[d.path] ?? ""; },
      () => { loading = null; });
  return loading;
}

const BY_PATH = new Map(DOCS.map((d) => [d.path, d]));

/** Normalise `../docs/x.md`, `./x.md`, `x.md` against the linking doc's folder. */
function joinPath(fromPath: string, href: string): string {
  const parts = fromPath.split("/").slice(0, -1);
  for (const seg of href.split("/")) {
    if (seg === "..") parts.pop();
    else if (seg && seg !== ".") parts.push(seg);
  }
  return parts.join("/");
}

/** What a link inside `from` points at. */
export function resolveDocLink(from: Doc, href: string): LinkTarget {
  if (/^https?:\/\//i.test(href)) return { kind: "web", href };
  if (href.startsWith("#")) return { kind: "anchor", anchor: href.slice(1) };
  if (/^[a-z]+:/i.test(href)) return { kind: "none" };          // mailto:, file:, …
  const [pathPart, anchor] = href.split("#");
  const doc = BY_PATH.get(joinPath(from.path, pathPart));
  return doc ? { kind: "doc", id: doc.id, anchor } : { kind: "none" };
}

/** Docs whose title, blurb or text mention every word of `query`. */
export function searchDocs(query: string): Doc[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return DOCS;
  return DOCS.filter((d) => {
    const hay = `${d.title} ${d.blurb} ${d.text}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}
