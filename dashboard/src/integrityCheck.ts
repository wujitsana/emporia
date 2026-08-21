/**
 * P23-4: browser-side ports of the integrity checks the Python SDK/MCP
 * server already do client-side -- `message_read.annotate_messages()`
 * (message_read.py) computes `verified` (Ed25519 signature check) and
 * `replayed` (per-page nonce dedup) using only public information: the
 * sender's *public* key, never a private one. Decryption is the only part
 * of that module that needs a private key, and this file deliberately does
 * not port it -- api.ts's own `looksLikeEncryptedEnvelope()` comment already
 * states the principle this respects: "the dashboard cannot decrypt and
 * never will". Verifying a signature or detecting a replay doesn't cross
 * that line; decrypting does.
 *
 * Also ports `rooms.compute_chain_hash()` for client-side room chain
 * verification (the "AuditBadge for rooms" SECURITY.md's P21 section
 * explicitly left as an opener for a later pass -- see its "Not built this
 * pass, and why").
 */

import { ed25519 } from "@noble/curves/ed25519.js";

// ─── Canonical signing payload (must byte-for-byte match identity.py) ────────

/**
 * Python's `sign()`/`verify()` (identity.py) serialize the signed payload as
 * `json.dumps(payload, sort_keys=True, separators=(",", ":"))` -- compact,
 * alphabetically-sorted keys, and critically `ensure_ascii=True` (the
 * `json.dumps` default): every non-ASCII character is escaped as `\uXXXX`
 * rather than emitted as a literal UTF-8 byte sequence. `JSON.stringify`
 * does the opposite (literal UTF-8, no non-ASCII escaping), so a message
 * containing anything outside printable ASCII (emoji, accented letters, any
 * non-Latin script) would serialize differently in JS than in Python and
 * verification would falsely fail on an honestly-signed message. This
 * escapes exactly the way Python's ASCII-only JSON encoder does, including
 * surrogate-pair handling for astral characters -- which falls out for free
 * here since a JS string already *is* a sequence of UTF-16 code units, the
 * same representation Python computes by hand for `\uXXXX` escaping.
 */
function pyJsonEscapeString(s: string): string {
  let out = "";
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    const code = s.charCodeAt(i);
    if (ch === '"') out += '\\"';
    else if (ch === "\\") out += "\\\\";
    else if (code === 0x08) out += "\\b";
    else if (code === 0x0c) out += "\\f";
    else if (code === 0x0a) out += "\\n";
    else if (code === 0x0d) out += "\\r";
    else if (code === 0x09) out += "\\t";
    else if (code < 0x20 || code > 0x7e) out += "\\u" + code.toString(16).padStart(4, "0");
    else out += ch;
  }
  return out;
}

type JsonValue = string | number | boolean | null | JsonValue[] | { [k: string]: JsonValue };

/** General `json.dumps(value, sort_keys=True, separators=(",", ":"))` port --
 * covers every payload shape this file needs to reconstruct:
 * `signed_room_message_payload()`/`signed_dm_thread_payload()` (flat
 * string dicts) and `federation_signable_bytes()`'s payload (a string field
 * plus a list of dicts containing strings and ints, used by the room-chain
 * signature below). Recurses so key order is sorted at every nesting level,
 * matching Python's `sort_keys=True` applying uniformly, not just at the top. */
function pyCanonicalJson(value: JsonValue): string {
  if (value === null) return "null";
  if (typeof value === "string") return `"${pyJsonEscapeString(value)}"`;
  if (typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  if (Array.isArray(value)) return `[${value.map(pyCanonicalJson).join(",")}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((k) => `"${pyJsonEscapeString(k)}":${pyCanonicalJson(value[k])}`).join(",")}}`;
}

/** `json.dumps(payload, sort_keys=True, separators=(",", ":"))` for a flat
 * string->string payload -- the shape `signed_room_message_payload()`/
 * `signed_dm_thread_payload()` (wire.py) produce. */
function canonicalSignPayload(payload: Record<string, string>): string {
  return pyCanonicalJson(payload);
}

function base64UrlToBytes(b64url: string): Uint8Array {
  const padded = b64url + "=".repeat((4 - (b64url.length % 4)) % 4);
  const b64 = padded.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function hexToBytes(hex: string): Uint8Array {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substring(i * 2, i * 2 + 2), 16);
  return bytes;
}

/** A message shape wide enough to cover both room and DM messages -- the
 * fields `annotate_messages()`/`verify_room_message()`/`verify_dm_message()`
 * actually consume. */
export interface SignableMessage {
  content: string;
  msg_type: string;
  nonce?: string | null;
  signature?: string | null;
}

/**
 * Mirrors `identity.verify_room_message()`/`verify_dm_message()`: `null`
 * (not `false`) when there's nothing to check -- a message stored before
 * signatures/nonces were required is honestly *unknown*, not tampered with.
 * `scopeId` is the `room_id` or `thread_id` the message was signed against
 * (`signed_room_message_payload`/`signed_dm_thread_payload` bind the
 * signature to it, same reason a content-only signature would replay across
 * rooms/threads forever otherwise).
 */
export function verifyMessageSignature(
  kind: "room" | "dm",
  scopeId: string,
  message: SignableMessage,
  senderPublicKeyHex: string,
): boolean | null {
  const { content, msg_type, nonce, signature } = message;
  if (!signature || !nonce || !senderPublicKeyHex) return null;
  const scopeKey = kind === "room" ? "room_id" : "thread_id";
  const payload: Record<string, string> = { [scopeKey]: scopeId, content, msg_type, nonce };
  try {
    const messageBytes = new TextEncoder().encode(canonicalSignPayload(payload));
    const sigBytes = base64UrlToBytes(signature);
    const pubKeyBytes = hexToBytes(senderPublicKeyHex);
    // zip215: false -- the stricter, RFC 8032-compatible verification mode,
    // matching the `cryptography` library's Ed25519PublicKey.verify() this
    // signature was originally checked against server-side (identity.py).
    // The two schemes agree on every honestly-generated signature; they can
    // disagree only on pathologically crafted ones, an edge case neither
    // side of this port specifically defends against.
    return ed25519.verify(sigBytes, messageBytes, pubKeyBytes, { zip215: false });
  } catch {
    return false;
  }
}

/**
 * Mirrors `annotate_messages()`'s replay tracking: a nonce seen earlier *in
 * this array* is flagged. Per-call state only, same standing limitation the
 * Python side documents -- a duplicate arriving on a later page/call isn't
 * detected. Pure -- does not mutate `messages`.
 */
export function markReplayedNonces<T extends { nonce?: string | null }>(
  messages: T[],
): (T & { replayed: boolean })[] {
  const seen = new Set<string>();
  return messages.map((m) => {
    const nonce = m.nonce || "";
    const replayed = Boolean(nonce) && seen.has(nonce);
    if (nonce) seen.add(nonce);
    return { ...m, replayed };
  });
}

// ─── Room chain verification (rooms.compute_chain_hash port) ─────────────────

/** `rooms.compute_chain_hash()`, ported: SHA256(f"{prev_hash or 'GENESIS'}:
 * {sender_id}:{content}:{created_at}"), hex digest. */
export async function computeChainHash(
  prevHash: string | null,
  senderId: string,
  content: string,
  createdAt: string,
): Promise<string> {
  const frame = `${prevHash || "GENESIS"}:${senderId}:${content}:${createdAt}`;
  const bytes = new TextEncoder().encode(frame);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export interface ChainMessage {
  sender_id: string;
  content: string;
  created_at: string;
  chain_hash?: string | null;
}

export interface ChainVerifyResult {
  verified: boolean;
  reason: string;
  head: string | null;
}

/**
 * Recompute a room's hash chain across `messages` (oldest first) and
 * compare each computed hash against its stored `chain_hash` -- the same
 * recomputation `rooms.verify_chain()`/`agent_sdk.py`'s `verify_room_chain()`
 * perform, applied to whatever page of messages the dashboard currently has
 * loaded (not a fetch-the-whole-room-from-genesis call; see
 * `pinRoomChainHead()` below for what makes this meaningful across reloads).
 */
export async function verifyChainOverMessages(
  messages: ChainMessage[],
  priorHead: string | null = null,
): Promise<ChainVerifyResult> {
  let prevHash = priorHead;
  for (const m of messages) {
    const expected = await computeChainHash(prevHash, m.sender_id, m.content, m.created_at);
    if (m.chain_hash !== expected) {
      return { verified: false, reason: "chain hash mismatch — history may have been rewritten", head: prevHash };
    }
    prevHash = expected;
  }
  return { verified: true, reason: `chain valid (${messages.length} messages)`, head: prevHash };
}

// ─── TOFU pinning (localStorage analogue of identity.load_room_chain_heads) ──

const PIN_STORAGE_PREFIX = "emporia.room_chain_pin.";

export interface RoomChainPin {
  chain_head: string;
  origin_public_key_hex: string;
}

/** Best-effort, matching identity.py's own file-based pin store contract:
 * a read/write failure (private browsing, disabled storage) degrades to
 * "no pin" rather than throwing -- pinning is a hardening measure, not a
 * correctness dependency the dashboard should break over. Stores both the
 * chain head AND the relay's signing key (same shape
 * load_room_chain_heads()/save_room_chain_heads() use), so a relay-identity
 * swap is caught the same way a content rewrite is. */
export function getPinnedRoomChainHead(pinKey: string): RoomChainPin | null {
  try {
    const raw = localStorage.getItem(PIN_STORAGE_PREFIX + pinKey);
    return raw ? (JSON.parse(raw) as RoomChainPin) : null;
  } catch {
    return null;
  }
}

export function setPinnedRoomChainHead(pinKey: string, pin: RoomChainPin): void {
  try {
    localStorage.setItem(PIN_STORAGE_PREFIX + pinKey, JSON.stringify(pin));
  } catch {
    // best-effort, same as the read side
  }
}

// ─── Full REST-based room chain verification (agent_sdk.py's verify_room_chain, ported) ──

function verifyRaw(dataBytes: Uint8Array, sigHex: string, pubkeyHex: string): boolean {
  try {
    return ed25519.verify(hexToBytes(sigHex), dataBytes, hexToBytes(pubkeyHex), { zip215: false });
  } catch {
    return false;
  }
}

export interface RoomChainVerifyResult {
  room_id: string;
  relay_reported_verified: boolean | null;
  signed_chain_head: string | null;
  message_count: number;
  verified: boolean;
  reason: string;
}

/**
 * Full port of `agent_sdk.py`'s `verify_room_chain()` (860-978): fetch the
 * relay-signed chain head, verify the signature came from the key the relay
 * claims, fetch the room's own message history (paginated, capped at
 * `maxMessages`), recompute the chain independently, and compare against a
 * head pinned in localStorage the last time this room verified cleanly. This
 * is the "materially different trust model" SECURITY.md's P21 section left
 * as an opener rather than a five-minute add-on -- a badge that only echoed
 * `roomChain.verified` (the relay's own self-report) would be the same
 * shallow guarantee `AuditBadge.tsx` already gives for sessions, not this.
 *
 * `fetchRoomChain`/`fetchRoomMessages` are injected (rather than importing
 * `api.ts` directly) so this module stays a pure, dependency-light unit
 * testable without a DOM/fetch mock -- callers pass `api.roomChain`/
 * `api.roomMessages` in practice.
 */
export async function verifyRoomChainFromRelay(
  relayUrl: string,
  roomId: string,
  fetchRoomChain: (roomId: string) => Promise<{
    verified: boolean; message: string; chain_head: string | null; message_count: number;
    origin_relay: string; origin_public_key_hex: string; signature: string;
  }>,
  fetchRoomMessages: (
    roomId: string, opts?: { limit?: number; before?: string },
  ) => Promise<{ messages: ChainMessage[] }>,
  maxMessages = 1000,
): Promise<RoomChainVerifyResult> {
  const data = await fetchRoomChain(roomId);
  const result: RoomChainVerifyResult = {
    room_id: roomId,
    relay_reported_verified: data.verified,
    signed_chain_head: data.chain_head,
    message_count: data.message_count,
    verified: false,
    reason: "",
  };

  const signable = pyCanonicalJson({
    origin_relay: data.origin_relay || relayUrl,
    room_chain: [{ room_id: roomId, chain_head: data.chain_head || "", message_count: data.message_count }],
  });
  const signatureValid = verifyRaw(new TextEncoder().encode(signable), data.signature, data.origin_public_key_hex);
  if (!signatureValid) {
    result.reason = "relay signature over the chain head did not verify";
    return result;
  }
  if (!data.verified) {
    result.reason = `relay's own recomputation failed: ${data.message}`;
    return result;
  }

  let rawMessages: ChainMessage[] = [];
  let before: string | undefined;
  const budget = Math.min(result.message_count, maxMessages);
  while (rawMessages.length < budget) {
    const pageLimit = Math.min(200, budget - rawMessages.length);
    const page = await fetchRoomMessages(roomId, { limit: pageLimit, before });
    const batch = page.messages ?? [];
    if (batch.length === 0) break;
    rawMessages = [...batch, ...rawMessages];
    before = batch[0].created_at;
    if (batch.length < pageLimit) break;
  }
  if (rawMessages.length < result.message_count) {
    result.reason =
      `could not fetch full history for independent verification — ` +
      `${rawMessages.length}/${result.message_count} messages within the ${maxMessages}-message cap`;
    return result;
  }

  let prevHash: string | null = null;
  const computedHeads: string[] = [];
  for (const m of rawMessages) {
    prevHash = await computeChainHash(prevHash, m.sender_id, m.content, m.created_at);
    computedHeads.push(prevHash);
  }
  if (prevHash !== data.chain_head) {
    result.reason = "recomputed chain head does not match the relay's signed head — the message content the relay served doesn't match what it signed";
    return result;
  }

  const pinKey = `${relayUrl}|${roomId}`;
  const pinned = getPinnedRoomChainHead(pinKey);
  if (pinned && pinned.chain_head && !computedHeads.includes(pinned.chain_head)) {
    result.reason = "previously pinned chain head is not an ancestor of the current chain — history was rewritten (reordered or deleted messages)";
    return result;
  }
  if (pinned && pinned.origin_public_key_hex && pinned.origin_public_key_hex !== data.origin_public_key_hex) {
    result.reason = "relay's signing key changed since it was last pinned for this room";
    return result;
  }

  setPinnedRoomChainHead(pinKey, { chain_head: prevHash || "", origin_public_key_hex: data.origin_public_key_hex });
  result.verified = true;
  result.reason = `chain valid and extends the previously pinned head (${rawMessages.length} messages)`;
  return result;
}
