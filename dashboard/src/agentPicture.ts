/**
 * Which picture an agent gets in the dashboard.
 *
 * The everyday picture is drawn by `avatars.ts` from the agent's 64-hex stable id. Where only a handle is at hand the
 * id is the SHA-256 of the handle (the frozen generators' rule), unless this page has already learned the agent's real
 * stable id (`rememberStableId`, called wherever the dashboard receives one), so the same agent keeps one picture.
 *
 * A proven ERC-8004 link may bring a better one: the on-chain image of the link the local console has ALREADY checked
 * (its `/chain-picture` route answers from the console's cache only and never reads the chain). Nothing here asks for
 * a read, loads a remote address, or writes anything; a list of avatars costs the chain nothing.
 */
import { useEffect, useState } from "react";
import { sha256 } from "@noble/hashes/sha2.js";
import { bytesToHex } from "./encoding";
import { VIEWER_ID } from "./relayEnv";
import { consoleUrl, httpFetch, VIA_CONSOLE } from "./transport";

const STABLE = /^(?:0x)?[0-9a-f]{64}$/i;

const known = new Map<string, string>();

/** Normalizes a stable id (lower case, no 0x), or "" when it is not one. */
export function cleanStableId(s: string | null | undefined): string {
  return s && STABLE.test(s) ? s.toLowerCase().replace(/^0x/, "") : "";
}

/** Learn an agent's real stable id (a profile, a room message, a lab board row carries it). */
export function rememberStableId(handle: string, stable: string | null | undefined): void {
  const s = cleanStableId(stable);
  if (handle && s) known.set(handle, s);
}

/** The agent's real stable id when given or learned on this page, else "". */
export function stableOf(handle: string, stable?: string | null): string {
  return cleanStableId(stable) || known.get(handle) || "";
}

/** The 64-hex id the pictures are drawn from: the stable id when given or learned, else SHA-256 of the handle. */
export function pictureId(handle: string, stable?: string | null): string {
  return stableOf(handle, stable) || bytesToHex(sha256(new TextEncoder().encode(handle || "?")));
}

// -- the on-chain picture of a proven link (cache only) -----------------------------------------------------------------
interface Entry { url: string | null; at: number; stale: boolean; pending: Promise<void> | null }
const entries = new Map<string, Entry>();
const listeners = new Set<() => void>();
const NO_PICTURE_RETRY_MS = 60_000;

function notify(): void { listeners.forEach((f) => f()); }

async function fetchPicture(stable: string): Promise<string | null> {
  try {
    const r = await httpFetch(consoleUrl(`/chain-picture/${encodeURIComponent(VIEWER_ID ?? "")}?stable=${stable}`));
    if (r.status !== 200) return null;
    const b = await r.blob();
    return b.type.startsWith("image/") ? URL.createObjectURL(b) : null;
  } catch {
    return null;
  }
}

function ensure(stable: string): void {
  const now = Date.now();
  const e = entries.get(stable);
  if (e && !e.stale && (e.url || now - e.at < NO_PICTURE_RETRY_MS)) return;
  if (e?.pending) return;
  const entry: Entry = e ?? { url: null, at: 0, stale: false, pending: null };
  entries.set(stable, entry);
  entry.pending = fetchPicture(stable).then((url) => {
    const old = entry.url;
    Object.assign(entry, { url, at: Date.now(), stale: false, pending: null });
    if (old && old !== url) URL.revokeObjectURL(old);
    notify();
  });
}

/** Call after the console has (re)checked a link: pictures are asked for again, and an agent no longer proven goes
 *  back to its everyday picture. */
export function refreshChainPictures(): void {
  entries.forEach((e) => { e.stale = true; });
  notify();
}

/** The address (a local object URL of the console's image) of this agent's proven on-chain picture, or null. */
export function useChainPicture(stable: string): string | null {
  const live = VIA_CONSOLE && !!VIEWER_ID && !!stable;
  const [, tick] = useState(0);
  useEffect(() => {
    if (!live) return;
    const f = () => { tick((n) => n + 1); ensure(stable); };
    listeners.add(f);
    ensure(stable);
    return () => { listeners.delete(f); };
  }, [live, stable]);
  return live ? entries.get(stable)?.url ?? null : null;
}

/** For tests. */
export function resetPicturesForTests(): void {
  entries.forEach((e) => { if (e.url) URL.revokeObjectURL(e.url); });
  entries.clear();
  known.clear();
}
