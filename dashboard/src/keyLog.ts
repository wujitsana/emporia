/**
 * P253: an agent's key log, read here as `wire.key_log_state` / `key_at` read it
 * (src/emporia/wire.py), so a message is checked against the key its sender had
 * when the relay recorded it — not against today's key, which failed every
 * message an agent signed before it rotated or recovered its key.
 *
 * A log is valid only from the agent's stable id: its first key must hash to
 * it, and every step must be signed as the Python side requires. An invalid log
 * gives no windows, and the caller falls back to the served key alone.
 */
import { ed25519 } from "@noble/curves/ed25519.js";
import { sha3_256 } from "@noble/hashes/sha3.js";
import { base64UrlToBytes, bytesToHex, hexToBytes } from "./encoding";
import { pyCanonicalJson } from "./pyJson";

export interface KeyWindow {
  key: string;
  from: string;
  until: string;
}

/** A sender's current key and, when its log is valid, the keys it held before. */
export interface SenderKey {
  key: string;
  windows: KeyWindow[];
}

/** wire.KEY_LOG_MAX_STEPS */
const KEY_LOG_MAX_STEPS = 100;

const HEX64 = /^[0-9a-f]{64}$/;
const isKeyHex = (v: unknown): v is string => typeof v === "string" && HEX64.test(v);

function prefixedHash(prefix: string, keyHex: string): string {
  const head = new TextEncoder().encode(prefix);
  const key = hexToBytes(keyHex.trim().toLowerCase());
  const data = new Uint8Array(head.length + key.length);
  data.set(head);
  data.set(key, head.length);
  return bytesToHex(sha3_256(data));
}

/** wire.stable_id_for */
export const stableIdFor = (publicKeyHex: string) => prefixedHash("emporia:agent:v1:", publicKeyHex);

/** wire.recovery_commitment_for */
export const recoveryCommitmentFor = (recoveryKeyHex: string) => prefixedHash("emporia:recovery:v1:", recoveryKeyHex);

/** wire.when_of, as microseconds since the epoch (Python keeps microseconds;
 *  a Date only milliseconds). An ISO time without a zone is UTC. */
export function whenOf(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return Math.round(value * 1e6);
  if (typeof value !== "string" || !value) return null;
  const m = /^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.(\d{1,6}))?)?(Z|[+-]\d{2}:?\d{2})?$/.exec(value);
  if (!m) return null;
  const zone = !m[4] || m[4] === "Z" ? "Z" : m[4].includes(":") ? m[4] : `${m[4].slice(0, 3)}:${m[4].slice(3)}`;
  const ms = Date.parse(`${m[1]}T${m[2] ?? "00:00"}${m[2] && m[2].length === 5 ? ":00" : ""}${zone}`);
  if (Number.isNaN(ms)) return null;
  return ms * 1000 + Number((m[3] ?? "").padEnd(6, "0"));
}

function verifySigned(payload: Record<string, string>, signature: string, keyHex: string): boolean {
  try {
    return ed25519.verify(
      base64UrlToBytes(signature), new TextEncoder().encode(pyCanonicalJson(payload)), hexToBytes(keyHex),
      { zip215: false },
    );
  } catch {
    return false;
  }
}

type Step = Record<string, unknown>;

/** wire.key_log_state's windows, or null when the log is not valid for this stable id. */
function keyLogState(stableId: string, log: unknown, currentKey: string): KeyWindow[] | null {
  if (!Array.isArray(log) || log.length > KEY_LOG_MAX_STEPS) return null;
  const firstOld = log.find((x): x is Step => !!x && typeof x === "object" && isKeyHex((x as Step).old));
  const genesis = firstOld ? (firstOld.old as string) : currentKey.toLowerCase();
  if (!isKeyHex(genesis) || stableIdFor(genesis) !== stableId) return null;
  let windows: KeyWindow[] = [{ key: genesis, from: "", until: "" }];
  const seen = new Set([genesis]);
  const commitments = new Set<string>();
  let live: { commitment: string; when: number } | null = null;
  let last: number | null = null;
  let lastRecovery: number | null = null;
  for (const raw of log) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const step = raw as Step;
    const at = step.at as string;
    const when = whenOf(at);
    const by = step.by ?? "key";
    if (when === null || (last !== null && when <= last) || typeof step.signature !== "string") return null;
    const signature = step.signature;
    const current = windows[windows.length - 1];
    if (by === "key") {
      const { old, new: next } = step;
      if (!isKeyHex(old) || !isKeyHex(next) || seen.has(next) || old !== current.key) return null;
      if (!verifySigned({ type: "emporia:agent-key-rotation:v1", stable_id: stableId, old, new: next, at },
        signature, old)) return null;
      current.until = at;
      windows.push({ key: next, from: at, until: "" });
      seen.add(next);
    } else if (by === "commit") {
      const { commitment } = step;
      if (!isKeyHex(commitment) || live !== null || commitments.has(commitment)) return null;
      if (!verifySigned({ type: "emporia:recovery-commitment:v1", stable_id: stableId, commitment, at },
        signature, current.key)) return null;
      commitments.add(commitment);
      live = { commitment, when };
    } else if (by === "recovery") {
      const { old, new: next, recovery_public_key: rk, compromised_since: since } = step;
      const sinceWhen = whenOf(since);
      if (!isKeyHex(old) || !isKeyHex(next) || !isKeyHex(rk) || sinceWhen === null || sinceWhen > when) return null;
      if (lastRecovery !== null && sinceWhen <= lastRecovery) return null;   // a recovery is never undone
      if (live === null || live.when >= sinceWhen || recoveryCommitmentFor(rk) !== live.commitment) return null;
      if (seen.has(next) || seen.has(rk) || next === rk) return null;
      const kept = windows.filter((w) => !w.from || (whenOf(w.from) ?? 0) < sinceWhen);
      if (kept[kept.length - 1].key !== old) return null;
      if (!verifySigned({
        type: "emporia:key-recovery:v1", stable_id: stableId, old, new: next, recovery_public_key: rk,
        compromised_since: since as string, at,
      }, signature, rk)) return null;
      windows = [...kept.slice(0, -1), { ...kept[kept.length - 1], until: since as string }, { key: next, from: at, until: "" }];
      seen.add(next);
      live = null;
      lastRecovery = when;
    } else {
      return null;
    }
    last = when;
  }
  return windows;
}

/** wire.key_log_windows: [] unless the log is valid for this stable id. */
export function keyLogWindows(stableId: string, log: unknown, currentKey = ""): KeyWindow[] {
  if (!Array.isArray(log) || log.length === 0) {
    const key = currentKey.toLowerCase();
    return isKeyHex(key) && stableIdFor(key) === stableId ? [{ key, from: "", until: "" }] : [];
  }
  return keyLogState(stableId, log, currentKey) ?? [];
}

/** wire.key_at: the key that was the agent's at `when`; "" when none was. No time means now. */
export function keyAt(windows: KeyWindow[], when: unknown = ""): string {
  if (windows.length === 0) return "";
  if (when === null || when === undefined || when === "") return windows[windows.length - 1].key;
  const moment = whenOf(when);
  if (moment === null) return "";
  for (const w of windows) {
    const start = whenOf(w.from);
    const end = whenOf(w.until);
    if ((start === null || moment >= start) && (end === null || moment < end)) return w.key;
  }
  return "";
}

/** What an agent's profile or key lookup gives, as a key that knows its log —
 *  the dashboard's `_sender_public_key`. The windows are kept only when the log
 *  is valid and ends at the key the relay serves now. */
export function senderKeyFrom(a: { public_key_hex?: string; stable_id?: string; key_log?: unknown }): SenderKey {
  const key = a.public_key_hex || "";
  const windows = a.stable_id && key ? keyLogWindows(a.stable_id, a.key_log ?? [], key) : [];
  return { key, windows: windows.length && windows[windows.length - 1].key === key ? windows : [] };
}

/** identity.signing_key_for: the key to check something signed at `when`. */
export function signingKeyFor(sender: SenderKey | string, when: unknown): string {
  if (typeof sender === "string") return sender;
  return sender.windows.length ? keyAt(sender.windows, when) : sender.key;
}
