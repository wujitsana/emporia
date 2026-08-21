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
  nonce?: string | null;
}

/** Either a bare `chain_hash` string (the *first* call in a walk, checked
 * for direct equality against the page's newest message's own stored hash)
 * or a pending message (every call after the first: the *previous*, newer
 * page's oldest message, unverifiable using that page alone, recomputed
 * against this page's newest as its true predecessor). See
 * `rooms.verify_chain_page()` (Python) for the full rationale -- this is a
 * hand port, kept in sync manually. */
export type ChainPageExpected = string | ChainMessage;

export interface ChainPageResult {
  ok: boolean;
  newExpected: ChainPageExpected | null;
  reason: string;
}

/**
 * TS port of `rooms.verify_chain_page()` (P24-1, W1) -- pure decision core
 * for one page of a *backward* (newest-to-oldest) room chain walk. `page`
 * must be ascending (oldest → newest), the same order GET
 * /rooms/{id}/messages already returns. See the Python function's docstring
 * for the full design rationale (why backward, why one message per page
 * boundary must be deferred, what `atGenesis` changes).
 */
export async function verifyChainPage(
  page: ChainMessage[],
  expected: ChainPageExpected,
  atGenesis: boolean,
): Promise<ChainPageResult> {
  if (page.length === 0) return { ok: false, newExpected: null, reason: "empty page" };

  const newest = page[page.length - 1];
  if (typeof expected === "string") {
    if (newest.chain_hash !== expected) {
      return {
        ok: false, newExpected: null,
        reason: "relay-signed chain head does not match the newest message's own stored hash — history may have been rewritten",
      };
    }
  } else {
    const pending = expected;
    const recomputed = await computeChainHash(newest.chain_hash ?? null, pending.sender_id, pending.content, pending.created_at);
    if (recomputed !== pending.chain_hash) {
      return {
        ok: false, newExpected: null,
        reason: "hash mismatch at a page boundary — either history was rewritten, or a message was lost at a created_at tie spanning the page split",
      };
    }
  }

  for (let i = 1; i < page.length; i++) {
    const older = page[i - 1];
    const newer = page[i];
    const recomputed = await computeChainHash(older.chain_hash ?? null, newer.sender_id, newer.content, newer.created_at);
    if (recomputed !== newer.chain_hash) {
      return { ok: false, newExpected: null, reason: "hash mismatch within a page — history may have been rewritten" };
    }
  }

  const oldest = page[0];
  if (atGenesis) {
    const genesisExpected = await computeChainHash(null, oldest.sender_id, oldest.content, oldest.created_at);
    if (genesisExpected !== oldest.chain_hash) {
      return {
        ok: false, newExpected: null,
        reason: "genesis message does not chain from an empty prior — chain does not anchor at a true beginning",
      };
    }
    return { ok: true, newExpected: null, reason: `genesis reached and verified (${page.length} messages in final page)` };
  }
  return { ok: true, newExpected: oldest, reason: `page verified (${page.length} messages), boundary check deferred` };
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
export interface PinnedRoomChainHeadResult {
  pin: RoomChainPin | null;
  /** P24-5 (W5, F4): a stored value that parses but isn't a well-formed
   *  RoomChainPin (corrupted localStorage, a foreign write to the same key)
   *  used to silently degrade to "no pin" -- indistinguishable from a room
   *  that has genuinely never been pinned, silently disabling the one check
   *  that catches a self-consistent history rewrite. `corrupt` lets the
   *  caller fail closed instead of reporting `verified: true` over an
   *  anchor that was never actually checked. */
  corrupt: boolean;
}

function isWellFormedPin(v: unknown): v is RoomChainPin {
  return (
    typeof v === "object" && v !== null && !Array.isArray(v) &&
    typeof (v as Record<string, unknown>).chain_head === "string" &&
    typeof (v as Record<string, unknown>).origin_public_key_hex === "string"
  );
}

export function getPinnedRoomChainHead(pinKey: string): PinnedRoomChainHeadResult {
  let raw: string | null;
  try {
    raw = localStorage.getItem(PIN_STORAGE_PREFIX + pinKey);
  } catch {
    return { pin: null, corrupt: false };
  }
  if (!raw) return { pin: null, corrupt: false };
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { pin: null, corrupt: true };
  }
  return isWellFormedPin(parsed) ? { pin: parsed, corrupt: false } : { pin: null, corrupt: true };
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
  /** P24-1 (W1): what "verified: true" actually means this call --
   *  "genesis" (walked all the way to the room's true first message this
   *  call) vs "pinned_checkpoint" (stopped at a hash already committed to
   *  its whole prefix by an earlier genesis walk). Never both; a badge that
   *  only rendered `verified` would overclaim exactly the way the old
   *  replay chip did (SECURITY.md). */
  chain_verified_scope: "genesis" | "pinned_checkpoint" | null;
  /** P24-2 (W2): how much of the room's nonce history this call actually
   *  scanned for a duplicate -- "full_history" only when this call reached
   *  true genesis; "since_pinned_head" when it stopped at the checkpoint
   *  (a duplicate whose original predates the pin is necessarily missed);
   *  "no_new_messages" when the pin fast-path skipped fetching anything. */
  replay_scan_scope: "full_history" | "since_pinned_head" | "no_new_messages" | null;
  replayed_nonce_count: number;
  replayed_nonce_sample: string[];
}

/**
 * TS port of `agent_sdk.py`'s `verify_room_chain()` (P21-7/P24-1, W1/W2) --
 * fetch the relay-signed chain head, verify the signature came from the key
 * the relay claims, then walk the room's own message history *backward*
 * (the only direction GET /rooms/{id}/messages paginates), verifying each
 * page against the one before it via `verifyChainPage()` above. See that
 * Python method's docstring for the full design rationale (full history by
 * default, pin-as-checkpoint, the replay scan riding the same walk) -- this
 * is a hand port, kept in sync manually rather than shared, the same
 * decision `pyCanonicalJson`/`computeChainHash` above already made.
 *
 * `fetchRoomChain`/`fetchRoomMessages` are injected (rather than importing
 * `api.ts` directly) so this module stays a pure, dependency-light unit
 * testable without a DOM/fetch mock -- callers pass `api.roomChain`/
 * `api.roomMessages` in practice.
 *
 * `maxMessages` defaults to a real bound (unlike the Python default of
 * unlimited) — each hash here is an async Web Crypto call on a UI that
 * polls, not a one-shot CLI/MCP call; pass `null` for an unbounded walk.
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
  maxMessages: number | null = 25_000,
  ignorePin = false,
): Promise<RoomChainVerifyResult> {
  const data = await fetchRoomChain(roomId);
  const result: RoomChainVerifyResult = {
    room_id: roomId,
    relay_reported_verified: data.verified,
    signed_chain_head: data.chain_head,
    message_count: data.message_count,
    verified: false,
    reason: "",
    chain_verified_scope: null,
    replay_scan_scope: null,
    replayed_nonce_count: 0,
    replayed_nonce_sample: [],
  };

  const signable = pyCanonicalJson({
    // P24-5 (W5, F5): `||` also falls back on a present-but-falsy value (an
    // empty string); Python's `data.get("origin_relay", relayUrl)` only
    // falls back when the key is actually *missing* -- diverging on a peer
    // that legitimately reports `origin_relay: ""` would recompute a
    // different signable payload than the relay signed and falsely reject.
    origin_relay: "origin_relay" in data ? data.origin_relay : relayUrl,
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

  if (result.message_count === 0) {
    result.verified = true;
    result.reason = "empty room — nothing to verify";
    result.chain_verified_scope = "genesis";
    result.replay_scan_scope = "full_history";
    return result;
  }

  const pinKey = `${relayUrl}|${roomId}`;
  const { pin: pinned, corrupt: pinCorrupt } = getPinnedRoomChainHead(pinKey);
  if (pinCorrupt) {
    // P24-5 (W5, F4): fail closed rather than silently treating a corrupt
    // stored pin the same as "never pinned" -- that used to report
    // `verified: true` over an anchor that was never actually checked,
    // exactly the TOFU guarantee this whole function exists to provide.
    result.reason = "stored chain-head pin is corrupt — refusing to report verified without a trustworthy TOFU anchor";
    return result;
  }

  if (!ignorePin && pinned && pinned.chain_head === data.chain_head) {
    if (pinned.origin_public_key_hex && pinned.origin_public_key_hex !== data.origin_public_key_hex) {
      result.reason = "relay's signing key changed since it was last pinned for this room";
      return result;
    }
    result.verified = true;
    result.reason = "chain head unchanged since the last pinned verification — nothing new to check";
    result.chain_verified_scope = "pinned_checkpoint";
    result.replay_scan_scope = "no_new_messages";
    return result;
  }

  const seenNonces = new Set<string>();
  const replaySample: string[] = [];
  let replayCount = 0;

  let expected: ChainPageExpected = data.chain_head || "";
  let before: string | undefined;
  let fetched = 0;
  let reachedGenesis = false;
  let reachedPin = false;
  let pinMatched = false;

  while (true) {
    if (maxMessages !== null && fetched >= maxMessages) {
      result.reason =
        `could not fetch full history for independent verification — ` +
        `${fetched}/${result.message_count} messages within the ${maxMessages}-message cap`;
      return result;
    }
    let pageLimit = Math.min(200, result.message_count - fetched);
    if (maxMessages !== null) pageLimit = Math.min(pageLimit, maxMessages - fetched);
    const page = await fetchRoomMessages(roomId, { limit: pageLimit, before });
    const batch = page.messages ?? [];
    if (batch.length < pageLimit) {
      result.reason = "relay served fewer messages than its own reported message_count claims exist";
      return result;
    }

    for (const m of batch) {
      if (!m.nonce) continue;
      if (seenNonces.has(m.nonce)) {
        replayCount++;
        if (replaySample.length < 20) replaySample.push(m.nonce);
      }
      seenNonces.add(m.nonce);
    }

    fetched += batch.length;
    const atGenesis = fetched >= result.message_count;

    // P24-1/2: the pin can land anywhere in a fetched page, not just at its
    // oldest boundary -- see agent_sdk.py's verify_room_chain() for why
    // this must scan the whole page.
    let pinIndex: number | null = null;
    if (pinned && !ignorePin) {
      for (let i = 0; i < batch.length; i++) {
        if (batch[i].chain_hash === pinned.chain_head) {
          pinIndex = i;
          break;
        }
      }
    }

    const verifyBatch = pinIndex !== null ? batch.slice(pinIndex) : batch;
    const verifyAtGenesis = atGenesis && pinIndex === null;

    const { ok, newExpected, reason } = await verifyChainPage(verifyBatch, expected, verifyAtGenesis);
    if (!ok) {
      result.reason = reason;
      return result;
    }

    if (pinIndex !== null) {
      pinMatched = true;
      if (pinned!.origin_public_key_hex && pinned!.origin_public_key_hex !== data.origin_public_key_hex) {
        result.reason = "relay's signing key changed since it was last pinned for this room";
        return result;
      }
      reachedPin = true;
      break;
    }

    if (atGenesis) {
      reachedGenesis = true;
      break;
    }

    const oldest = batch[0];
    expected = newExpected as ChainPageExpected;
    before = oldest.created_at;
  }

  if (reachedGenesis && pinned && !pinMatched && !ignorePin) {
    result.reason = "previously pinned chain head is not an ancestor of the current chain — history was rewritten (reordered or deleted messages)";
    return result;
  }

  result.verified = true;
  result.replayed_nonce_count = replayCount;
  result.replayed_nonce_sample = replaySample;
  if (reachedGenesis) {
    result.chain_verified_scope = "genesis";
    result.replay_scan_scope = "full_history";
    result.reason = `chain valid, verified from true genesis this call (${fetched} messages)`;
  } else {
    result.chain_verified_scope = "pinned_checkpoint";
    result.replay_scan_scope = "since_pinned_head";
    result.reason =
      `chain valid back to the previously pinned head (${fetched} new messages checked this call; ` +
      `earlier history was verified from genesis when that head was first pinned)`;
  }

  setPinnedRoomChainHead(pinKey, { chain_head: data.chain_head || "", origin_public_key_hex: data.origin_public_key_hex });
  return result;
}
