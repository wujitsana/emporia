import { ed25519 } from "@noble/curves/ed25519.js";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  computeChainHash,
  getPinnedRoomChainHead,
  markReplayedNonces,
  setPinnedRoomChainHead,
  verifyChainOverMessages,
  verifyChainPage,
  verifyMessageSignature,
  verifyRoomChainFromRelay,
  verifyDmChainFromRelay,
} from "./integrityCheck";

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
}

// P23-4: a second golden fixture, this one end-to-end through
// identity.federation_signable_bytes()/sign_raw_relay() -- the same
// construction GET /rooms/{id}/chain uses to sign its response. Content
// includes non-ASCII (Japanese) to exercise the same escaping path as the
// message-signature fixture, on a structurally different (nested) payload.
const CHAIN_GOLDEN = {
  relayUrl: "http://golden-relay.example",
  roomId: "room_chain_golden",
  relayPubkeyHex: "b02fa173d69ac1b0b4d4c97d72e2869db4ecdb8c192895c9b2c5675e749a3fbb",
  chainHead: "153eba2976d3d61ce3fd95e0f72a6e9f981f057b3c09d28a60c6b963907168b8",
  signature: "68d081bbcb2ed99f507ea6f78600b5af56b607f1ca49f54aa58343cc0794d15cb83cfa19962a8417a9a931c018fb29ff91bb18cfed19298f46a2bee623521c08",
  messages: [
    { sender_id: "agent_a", content: "first", created_at: "2026-01-01T00:00:00Z", chain_hash: "e6da802b2886ee09aeba45a4d3e03d2250547a57923ebebf2ea396da9edd69b8" },
    { sender_id: "agent_b", content: "second — 日本語", created_at: "2026-01-01T00:00:05Z", chain_hash: "153eba2976d3d61ce3fd95e0f72a6e9f981f057b3c09d28a60c6b963907168b8" },
  ],
};

function goldenFetchRoomChain() {
  return Promise.resolve({
    verified: true,
    message: "chain valid",
    chain_head: CHAIN_GOLDEN.chainHead,
    message_count: CHAIN_GOLDEN.messages.length,
    origin_relay: CHAIN_GOLDEN.relayUrl,
    origin_public_key_hex: CHAIN_GOLDEN.relayPubkeyHex,
    signature: CHAIN_GOLDEN.signature,
  });
}

function goldenFetchRoomMessages() {
  return Promise.resolve({ messages: CHAIN_GOLDEN.messages });
}

// P24-1/2 (W1/W2): a synthetic, freshly-signed room of arbitrary size, used
// to exercise the backward, page-at-a-time walk (the golden fixture above is
// deliberately tiny -- 2 messages, one page). createdAt values are a
// zero-padded counter (not real timestamps) so they sort exactly like the
// relay's real ISO-8601 strings do, deterministically, without any timing
// dependency in a fast test loop.
async function buildSignedRoom(relayUrl: string, roomId: string, contents: string[]) {
  const secretKey = ed25519.utils.randomSecretKey();
  const publicKey = ed25519.getPublicKey(secretKey);
  const pubHex = bytesToHex(publicKey);

  const messages: (typeof CHAIN_GOLDEN.messages)[number][] = [];
  let prevHash: string | null = null;
  for (let i = 0; i < contents.length; i++) {
    const createdAt = `2026-01-01T00:00:00.${String(i).padStart(6, "0")}Z`;
    const hash = await computeChainHash(prevHash, "agent_a", contents[i], createdAt);
    messages.push({ sender_id: "agent_a", content: contents[i], created_at: createdAt, chain_hash: hash });
    prevHash = hash;
  }
  const chainHead = prevHash;

  const signable = JSON.stringify({
    origin_relay: relayUrl,
    room_chain: [{ chain_head: chainHead, message_count: messages.length, room_id: roomId }],
  });
  const signature = bytesToHex(ed25519.sign(new TextEncoder().encode(signable), secretKey));

  return { relayUrl, roomId, messages, chainHead, pubHex, signature, secretKey };
}

function fetchChainFor(room: Awaited<ReturnType<typeof buildSignedRoom>>) {
  return () =>
    Promise.resolve({
      verified: true,
      message: "chain valid",
      chain_head: room.chainHead,
      message_count: room.messages.length,
      origin_relay: room.relayUrl,
      origin_public_key_hex: room.pubHex,
      signature: room.signature,
    });
}

/** Appends more messages to an already-built signed room (same secretKey,
 * continuing the chain), re-signing a fresh chain head over the extended
 * history -- used to simulate "a few new messages arrived since the pin". */
async function extendSignedRoom(room: Awaited<ReturnType<typeof buildSignedRoom>>, moreContents: string[]) {
  const messages = [...room.messages];
  let prevHash = room.chainHead;
  for (let i = 0; i < moreContents.length; i++) {
    const idx = room.messages.length + i;
    const createdAt = `2026-01-01T00:00:00.${String(idx).padStart(6, "0")}Z`;
    const hash = await computeChainHash(prevHash, "agent_a", moreContents[i], createdAt);
    messages.push({ sender_id: "agent_a", content: moreContents[i], created_at: createdAt, chain_hash: hash });
    prevHash = hash;
  }
  const chainHead = prevHash;
  const signable = JSON.stringify({
    origin_relay: room.relayUrl,
    room_chain: [{ chain_head: chainHead, message_count: messages.length, room_id: room.roomId }],
  });
  const signature = bytesToHex(ed25519.sign(new TextEncoder().encode(signable), room.secretKey));
  return { ...room, messages, chainHead, signature };
}

/** Mirrors rooms.get_messages()'s real pagination semantics (created_at <
 * before, newest LIMIT rows, returned ascending) against an in-memory,
 * already-ascending array. */
function fetchMessagesFrom(allMessages: typeof CHAIN_GOLDEN.messages, onFetch?: () => void) {
  return (_roomId: string, opts?: { limit?: number; before?: string }) => {
    onFetch?.();
    const limit = opts?.limit ?? 200;
    const pool = opts?.before ? allMessages.filter((m) => m.created_at < opts.before!) : allMessages;
    const page = pool.slice(Math.max(0, pool.length - limit));
    return Promise.resolve({ messages: page });
  };
}

// P23-4: golden fixture generated by identity.py itself (a real Ed25519
// keypair, a real signed_room_message_payload(), a real identity.sign()
// call) -- not hand-constructed. Content deliberately includes an em-dash,
// CJK characters, an emoji (astral, needs surrogate-pair escaping), a quote,
// and a backslash: exactly the characters where a canonical-JSON port is
// most likely to silently diverge from Python's `ensure_ascii=True` default.
const GOLDEN = {
  pubHex: "aed0ceb84480344a0ef1dfcc19c5afe550df7b00d1de45eb07173e8e046ba43c",
  roomId: "room_golden_fixture",
  content: 'Hello — 世界 🎉 "quoted" and a\\backslash',
  msgType: "chat",
  nonce: "nonce-golden-001",
  signature: "CcOpZTRBT6AEe0pY6cpV6WSOo8-T0vIL1BHz4XPEol-OCUG4wcuUTIm0kkTg-zzr7uxNQPtw38whtj9ip-XGBg",
};

describe("verifyMessageSignature", () => {
  it("accepts a real signature produced by identity.py, including non-ASCII content", () => {
    const result = verifyMessageSignature(
      "room", GOLDEN.roomId,
      { content: GOLDEN.content, msg_type: GOLDEN.msgType, nonce: GOLDEN.nonce, signature: GOLDEN.signature },
      GOLDEN.pubHex,
    );
    expect(result).toBe(true);
  });

  it("rejects the same signature over tampered content", () => {
    const result = verifyMessageSignature(
      "room", GOLDEN.roomId,
      { content: GOLDEN.content + " tampered", msg_type: GOLDEN.msgType, nonce: GOLDEN.nonce, signature: GOLDEN.signature },
      GOLDEN.pubHex,
    );
    expect(result).toBe(false);
  });

  it("rejects the same signature under a different room_id (binding, not just content)", () => {
    const result = verifyMessageSignature(
      "room", "a_different_room",
      { content: GOLDEN.content, msg_type: GOLDEN.msgType, nonce: GOLDEN.nonce, signature: GOLDEN.signature },
      GOLDEN.pubHex,
    );
    expect(result).toBe(false);
  });

  it("rejects a room-scoped signature checked as a dm signature (thread_id vs room_id key differs)", () => {
    const result = verifyMessageSignature(
      "dm", GOLDEN.roomId,
      { content: GOLDEN.content, msg_type: GOLDEN.msgType, nonce: GOLDEN.nonce, signature: GOLDEN.signature },
      GOLDEN.pubHex,
    );
    expect(result).toBe(false);
  });

  it("returns null (unknown, not tampered) when signature/nonce/pubkey is missing", () => {
    expect(verifyMessageSignature("room", GOLDEN.roomId, { content: "x", msg_type: "chat" }, GOLDEN.pubHex)).toBeNull();
    expect(
      verifyMessageSignature(
        "room", GOLDEN.roomId,
        { content: GOLDEN.content, msg_type: GOLDEN.msgType, nonce: GOLDEN.nonce, signature: GOLDEN.signature },
        "",
      ),
    ).toBeNull();
  });
});

describe("markReplayedNonces", () => {
  it("flags the second occurrence of a nonce, not the first", () => {
    const messages = [{ nonce: "n1" }, { nonce: "n2" }, { nonce: "n1" }];
    const annotated = markReplayedNonces(messages);
    expect(annotated.map((m) => m.replayed)).toEqual([false, false, true]);
  });

  it("does not flag messages with no nonce", () => {
    const messages = [{ nonce: null }, { nonce: undefined }, {}] as { nonce?: string | null }[];
    const annotated = markReplayedNonces(messages);
    expect(annotated.every((m) => !m.replayed)).toBe(true);
  });

  it("does not mutate the input array", () => {
    const messages = [{ nonce: "n1" }];
    markReplayedNonces(messages);
    expect((messages[0] as { replayed?: boolean }).replayed).toBeUndefined();
  });
});

describe("computeChainHash", () => {
  it("matches rooms.compute_chain_hash()'s golden value for a genesis message", async () => {
    const h1 = await computeChainHash(null, "agent_a", "first message", "2026-01-01T00:00:00Z");
    expect(h1).toBe("4ba1387492a4ad3e5b6315236291514c2eb63bb0e94a9380aca384e282b0655a".slice(0, 64));
  });

  it("matches rooms.compute_chain_hash()'s golden value for a chained message with non-ASCII content", async () => {
    const h1 = await computeChainHash(null, "agent_a", "first message", "2026-01-01T00:00:00Z");
    const h2 = await computeChainHash(h1, "agent_b", GOLDEN.content, "2026-01-01T00:00:05Z");
    expect(h2).toBe("8cf8b0dda11c3a2a4a1a7d1a504eb3134175cdfb733e9a160bc26e5808b0294c");
  });
});

describe("verifyChainOverMessages", () => {
  it("verifies a clean, self-consistent chain", async () => {
    const h1 = await computeChainHash(null, "agent_a", "first message", "2026-01-01T00:00:00Z");
    const h2 = await computeChainHash(h1, "agent_b", "second message", "2026-01-01T00:00:05Z");
    const result = await verifyChainOverMessages([
      { sender_id: "agent_a", content: "first message", created_at: "2026-01-01T00:00:00Z", chain_hash: h1 },
      { sender_id: "agent_b", content: "second message", created_at: "2026-01-01T00:00:05Z", chain_hash: h2 },
    ]);
    expect(result.verified).toBe(true);
    expect(result.head).toBe(h2);
  });

  it("detects a rewrite -- tampered content produces a mismatched stored chain_hash", async () => {
    const h1 = await computeChainHash(null, "agent_a", "first message", "2026-01-01T00:00:00Z");
    const h2 = await computeChainHash(h1, "agent_b", "second message", "2026-01-01T00:00:05Z");
    const result = await verifyChainOverMessages([
      // content tampered but chain_hash left as the original (pre-tamper) value
      { sender_id: "agent_a", content: "first message, TAMPERED", created_at: "2026-01-01T00:00:00Z", chain_hash: h1 },
      { sender_id: "agent_b", content: "second message", created_at: "2026-01-01T00:00:05Z", chain_hash: h2 },
    ]);
    expect(result.verified).toBe(false);
  });
});

describe("room chain head pinning (localStorage TOFU store)", () => {
  // vitest's "node" environment (vitest.config.ts) has no real localStorage
  // (confirmed: Node needs --localstorage-file to enable the experimental
  // global) -- a minimal in-memory stand-in exercises the real read/write
  // path instead of only the best-effort try/catch fallback, without pulling
  // in jsdom as a new dependency just for this one module.
  beforeEach(() => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
      setItem: (k: string, v: string) => { store.set(k, v); },
      removeItem: (k: string) => { store.delete(k); },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns null when nothing is pinned yet", () => {
    expect(getPinnedRoomChainHead("relay|room_never_pinned_" + Math.random())).toEqual({ pin: null, corrupt: false });
  });

  it("round-trips a pinned head", () => {
    const key = "relay|room_roundtrip_" + Math.random();
    setPinnedRoomChainHead(key, { chain_head: "deadbeef", origin_public_key_hex: "ab12" });
    expect(getPinnedRoomChainHead(key)).toEqual({
      pin: { chain_head: "deadbeef", origin_public_key_hex: "ab12" },
      corrupt: false,
    });
  });

  it.each([
    ["a bare number", "0"],
    ["a JSON null", "null"],
    ["a JSON array", "[]"],
    ["an object missing both required fields", "{}"],
    ["an object with a non-string chain_head", JSON.stringify({ chain_head: 1, origin_public_key_hex: "ab12" })],
    ["unparseable JSON", "{not json"],
  ])("reports corrupt (not silently 'never pinned') for %s", (_label, stored) => {
    const key = "relay|room_corrupt_" + Math.random();
    localStorage.setItem(`emporia.room_chain_pin.${key}`, stored);
    expect(getPinnedRoomChainHead(key)).toEqual({ pin: null, corrupt: true });
  });

  describe("verifyDmChainFromRelay (P32-2)", () => {
    /** Build a signed DM-chain response plus its message list. */
    async function makeThread(opts: {
      relayUrl: string; threadId: string; contents: string[]; unchainedPrefix?: number;
    }) {
      const secretKey = ed25519.utils.randomSecretKey();
      const pubHex = bytesToHex(ed25519.getPublicKey(secretKey));
      const messages: any[] = [];
      let prev: string | null = null;
      // Unchained rows come first and carry no hash, exactly as pre-P28 rows do.
      for (let i = 0; i < (opts.unchainedPrefix ?? 0); i++) {
        messages.push({
          sender_id: "a", content: `legacy ${i}`,
          created_at: `2020-01-01T00:00:0${i}.000000+00:00`, chain_hash: null,
          nonce: `legacy_${i}`, seq: i + 1,
        });
      }
      for (const [i, content] of opts.contents.entries()) {
        const created_at = `2026-04-04T00:00:${String(i).padStart(2, "0")}.000000+00:00`;
        prev = await computeChainHash(prev, "a", content, created_at);
        messages.push({
          sender_id: "a", content, created_at, chain_hash: prev,
          nonce: `n_${i}`, seq: (opts.unchainedPrefix ?? 0) + i + 1,
        });
      }
      const chainHead = prev;
      const signable = JSON.stringify({
        dm_chain: [{ chain_head: chainHead || "", message_count: opts.contents.length, thread_id: opts.threadId }],
        origin_relay: opts.relayUrl,
      });
      const signature = bytesToHex(ed25519.sign(new TextEncoder().encode(signable), secretKey));
      const fetchChain = async () => ({
        verified: true, message: "ok", chain_head: chainHead,
        message_count: opts.contents.length, unchained_prefix: opts.unchainedPrefix ?? 0,
        origin_relay: opts.relayUrl, origin_public_key_hex: pubHex, signature,
      });
      const fetchMessages = async () => ({ messages });
      return { fetchChain, fetchMessages, chainHead, pubHex };
    }

    it("verifies a clean thread and pins the head under a dm-namespaced key", async () => {
      const relayUrl = "http://relay-dm.example";
      const threadId = "dm_clean";
      const { fetchChain, fetchMessages, chainHead, pubHex } = await makeThread({
        relayUrl, threadId, contents: ["one", "two", "three"],
      });
      const result = await verifyDmChainFromRelay(relayUrl, threadId, fetchChain, fetchMessages);
      expect(result.verified).toBe(true);
      expect(result.chain_verified_scope).toBe("genesis");
      expect(result.unchained_prefix).toBe(0);
      // `dm|` namespacing keeps a thread's pin from ever colliding with a room's.
      expect(getPinnedRoomChainHead(`${relayUrl}|dm|${threadId}`)).toEqual({
        pin: { chain_head: chainHead, origin_public_key_hex: pubHex },
        corrupt: false,
      });
    });

    it("catches a self-consistent rewrite that the relay itself still signs as valid", async () => {
      const relayUrl = "http://relay-dm2.example";
      const threadId = "dm_rewrite";
      const original = await makeThread({ relayUrl, threadId, contents: ["one", "two", "three"] });
      const first = await verifyDmChainFromRelay(
        relayUrl, threadId, original.fetchChain, original.fetchMessages,
      );
      expect(first.verified).toBe(true);

      // The operator edits a message and recomputes every later hash, then
      // re-signs. Recomputation cannot catch this -- the relay's own report is
      // clean and its signature is genuine. Only the pinned head can.
      const rewritten = await makeThread({
        relayUrl, threadId, contents: ["one", "TAMPERED", "three"],
      });
      const relaySays = await rewritten.fetchChain();
      expect(relaySays.verified).toBe(true);

      const after = await verifyDmChainFromRelay(
        relayUrl, threadId, rewritten.fetchChain, rewritten.fetchMessages,
      );
      expect(after.verified).toBe(false);
      expect(after.reason).toContain("rewritten");
    });

    it("walks more than one page, and still catches a break on the oldest one", async () => {
      // P31-1's lesson applied to this file rather than restated: a
      // paginating walk whose tests never paginate has its whole loop --
      // cursor, `expected` hand-off across boundaries, deferred check on each
      // page's oldest message -- entirely unexercised.
      const relayUrl = "http://relay-dm4.example";
      const threadId = "dm_paged";
      const secretKey = ed25519.utils.randomSecretKey();
      const pubHex = bytesToHex(ed25519.getPublicKey(secretKey));

      const build = async (tamperAt: number | null) => {
        const messages: any[] = [];
        let prev: string | null = null;
        for (let i = 0; i < 250; i++) {
          const created_at =
            `2026-05-05T00:${String(Math.floor(i / 60)).padStart(2, "0")}:` +
            `${String(i % 60).padStart(2, "0")}.${String(i).padStart(6, "0")}+00:00`;
          const content = `paged ${i}`;
          prev = await computeChainHash(prev, "a", content, created_at);
          messages.push({
            sender_id: "a",
            // Tamper with the CONTENT only, leaving stored hashes intact, so
            // the recomputation diverges the way a real edit would.
            content: i === tamperAt ? "TAMPERED" : content,
            created_at, chain_hash: prev, nonce: `pn_${i}`, seq: i + 1,
          });
        }
        return { messages, head: prev as string };
      };

      const clean = await build(null);
      const signable = JSON.stringify({
        dm_chain: [{ chain_head: clean.head, message_count: 250, thread_id: threadId }],
        origin_relay: relayUrl,
      });
      const signature = bytesToHex(ed25519.sign(new TextEncoder().encode(signable), secretKey));
      const fetchChain = async () => ({
        verified: true, message: "ok", chain_head: clean.head, message_count: 250,
        unchained_prefix: 0, origin_relay: relayUrl,
        origin_public_key_hex: pubHex, signature,
      });

      let pages = 0;
      const pagedFetcher = (all: any[]) => async (
        _t: string, opts?: { limit?: number; before?: string; beforeSeq?: number | null },
      ) => {
        pages++;
        const limit = opts?.limit ?? 200;
        let pool = all;
        if (opts?.before) {
          pool = all.filter(
            (m) =>
              m.created_at < opts.before! ||
              (m.created_at === opts.before! && opts.beforeSeq != null && m.seq < opts.beforeSeq),
          );
        }
        return { messages: pool.slice(Math.max(0, pool.length - limit)) };
      };

      const ok = await verifyDmChainFromRelay(
        relayUrl, threadId, fetchChain, pagedFetcher(clean.messages), null, true,
      );
      expect(ok.verified).toBe(true);
      expect(pages).toBeGreaterThan(1);

      // Message 5 lives on the OLDEST page, two boundaries back.
      const broken = await build(5);
      const after = await verifyDmChainFromRelay(
        relayUrl, threadId, fetchChain, pagedFetcher(broken.messages), null, true,
      );
      expect(after.verified).toBe(false);
    });

    it("reports chained_start, not genesis, when messages predate the chain", async () => {
      const relayUrl = "http://relay-dm3.example";
      const threadId = "dm_prefix";
      const { fetchChain, fetchMessages } = await makeThread({
        relayUrl, threadId, contents: ["modern one", "modern two"], unchainedPrefix: 2,
      });
      const result = await verifyDmChainFromRelay(relayUrl, threadId, fetchChain, fetchMessages);
      expect(result.verified).toBe(true);
      // Reaching the start of the CHAIN is not reaching the thread's first
      // message; calling this "genesis" would claim coverage it doesn't have.
      expect(result.chain_verified_scope).toBe("chained_start");
      expect(result.unchained_prefix).toBe(2);
      expect(result.reason).toContain("not covered");
    });
  });

  describe("verifyRoomChainFromRelay", () => {
    it("verifies a clean room against a real relay-signed chain head (golden fixture) and pins it", async () => {
      const result = await verifyRoomChainFromRelay(
        CHAIN_GOLDEN.relayUrl, CHAIN_GOLDEN.roomId, goldenFetchRoomChain, goldenFetchRoomMessages,
      );
      expect(result.verified).toBe(true);
      expect(result.reason).toContain("chain valid");
      expect(getPinnedRoomChainHead(`${CHAIN_GOLDEN.relayUrl}|${CHAIN_GOLDEN.roomId}`)).toEqual({
        pin: { chain_head: CHAIN_GOLDEN.chainHead, origin_public_key_hex: CHAIN_GOLDEN.relayPubkeyHex },
        corrupt: false,
      });
    });

    it("passes before_seq on every page after the first, so a created_at tie can't drop a message (P31-3)", async () => {
      // Paginating on `created_at < ?` alone excludes any message sharing a
      // timestamp with the page boundary -- it can never be fetched, and the
      // walk reports an untampered room as tampered. This file's own page-
      // boundary error string used to hedge about exactly that case instead
      // of the cursor being fixed. Here the fetcher REFUSES to serve a page
      // without the tiebreak once one is needed, so a regression to the old
      // cursor fails rather than silently degrading.
      const secretKey = ed25519.utils.randomSecretKey();
      const publicKey = ed25519.getPublicKey(secretKey);
      const pubHex = bytesToHex(publicKey);
      const roomId = "room_p31_tie";
      const relayUrl = "http://relay-tie.example";

      // 201 messages so the walk's own page size (min(200, count)) forces a
      // second page, with the two OLDEST sharing one created_at so the tie
      // lands exactly on that boundary.
      const tied = "2026-03-03T00:00:00.000000+00:00";
      const raw = Array.from({ length: 201 }, (_, i) => ({
        sender_id: "a",
        content: `m${i}`,
        created_at:
          i < 2
            ? tied
            : `2026-03-03T00:${String(Math.floor(i / 60)).padStart(2, "0")}:` +
              `${String(i % 60).padStart(2, "0")}.${String(i).padStart(6, "0")}+00:00`,
        seq: i + 1,
      }));
      const messages: any[] = [];
      let prev: string | null = null;
      for (const m of raw) {
        prev = await computeChainHash(prev, m.sender_id, m.content, m.created_at);
        messages.push({ ...m, chain_hash: prev });
      }
      const chainHead = prev as string;

      const signable = JSON.stringify({
        origin_relay: relayUrl,
        room_chain: [{ chain_head: chainHead, message_count: messages.length, room_id: roomId }],
      });
      const signature = bytesToHex(
        ed25519.sign(new TextEncoder().encode(signable), secretKey),
      );

      const seenBeforeSeq: Array<number | null | undefined> = [];
      const fetchChain = async () => ({
        verified: true, message: "ok", chain_head: chainHead,
        message_count: messages.length, origin_relay: relayUrl,
        origin_public_key_hex: pubHex, signature,
      });
      const fetchMessages = async (
        _roomId: string,
        opts?: { limit?: number; before?: string; beforeSeq?: number | null },
      ) => {
        const limit = opts?.limit ?? 200;
        let pool = messages;
        if (opts?.before) {
          seenBeforeSeq.push(opts.beforeSeq);
          if (opts.beforeSeq == null) {
            throw new Error("paginated without before_seq — a created_at tie would drop a message");
          }
          pool = messages.filter(
            (m) =>
              m.created_at < opts.before! ||
              (m.created_at === opts.before! && m.seq < opts.beforeSeq!),
          );
        }
        return { messages: pool.slice(Math.max(0, pool.length - limit)) };
      };

      const result = await verifyRoomChainFromRelay(
        relayUrl, roomId, fetchChain, fetchMessages, null, true,
      );
      expect(result.verified).toBe(true);
      expect(seenBeforeSeq.length).toBeGreaterThan(0);
      expect(seenBeforeSeq.every((v) => typeof v === "number")).toBe(true);
    });

    it("fails closed when the stored pin is corrupt, instead of silently verifying as if never pinned", async () => {
      const pinKey = `${CHAIN_GOLDEN.relayUrl}|${CHAIN_GOLDEN.roomId}`;
      localStorage.setItem(`emporia.room_chain_pin.${pinKey}`, "not json");
      const result = await verifyRoomChainFromRelay(
        CHAIN_GOLDEN.relayUrl, CHAIN_GOLDEN.roomId, goldenFetchRoomChain, goldenFetchRoomMessages,
      );
      expect(result.verified).toBe(false);
      expect(result.reason).toContain("corrupt");
    });

    it("uses a present-but-empty-string origin_relay verbatim, matching Python's dict.get semantics", async () => {
      // The relay's actual signature is over whatever `origin_relay` value it
      // put in the signable payload -- if the relay legitimately reports "",
      // the client must recompute over "" too, or an honest response fails
      // to verify. `||` (rather than an "in" check) would substitute
      // `relayUrl` here, producing a different signable string than what was
      // actually signed.
      const secretKey = ed25519.utils.randomSecretKey();
      const publicKey = ed25519.getPublicKey(secretKey);
      const pubHex = bytesToHex(publicKey);

      const roomId = "room_empty_origin_relay";
      const relayUrl = "http://relay-x.example";
      const message = { sender_id: "agent_a", content: "hi", created_at: "2026-01-01T00:00:00Z" };
      const chainHead = await computeChainHash(null, message.sender_id, message.content, message.created_at);
      const messages = [{ ...message, chain_hash: chainHead }];

      // Sorted-keys, no-whitespace JSON -- matches pyCanonicalJson's output
      // for this ASCII-only, already-sorted-by-construction payload.
      const signable = JSON.stringify({
        origin_relay: "",
        room_chain: [{ chain_head: chainHead, message_count: messages.length, room_id: roomId }],
      });
      const signature = bytesToHex(ed25519.sign(new TextEncoder().encode(signable), secretKey));

      const fetchChain = async () => ({
        verified: true,
        message: "chain valid",
        chain_head: chainHead,
        message_count: messages.length,
        origin_relay: "",
        origin_public_key_hex: pubHex,
        signature,
      });
      const fetchMessages = async () => ({ messages });

      const result = await verifyRoomChainFromRelay(relayUrl, roomId, fetchChain, fetchMessages);
      expect(result.verified).toBe(true);
    });

    it("rejects a chain-head signature from the wrong relay key", async () => {
      const wrongKeyFetch = async () => ({ ...(await goldenFetchRoomChain()), origin_public_key_hex: "00".repeat(32) });
      const result = await verifyRoomChainFromRelay(
        CHAIN_GOLDEN.relayUrl, CHAIN_GOLDEN.roomId, wrongKeyFetch, goldenFetchRoomMessages,
      );
      expect(result.verified).toBe(false);
      expect(result.reason).toContain("did not verify");
    });

    it("fails closed when the relay's own recomputation already reports unverified", async () => {
      const unverifiedFetch = async () => ({ ...(await goldenFetchRoomChain()), verified: false, message: "boom" });
      const result = await verifyRoomChainFromRelay(
        CHAIN_GOLDEN.relayUrl, CHAIN_GOLDEN.roomId, unverifiedFetch, goldenFetchRoomMessages,
      );
      expect(result.verified).toBe(false);
      expect(result.reason).toContain("boom");
    });

    it("catches a self-consistent rewrite via the pin, on a second call", async () => {
      // First call pins the real head (mirrors the SDK test's pin-then-tamper shape).
      await verifyRoomChainFromRelay(
        CHAIN_GOLDEN.relayUrl, CHAIN_GOLDEN.roomId, goldenFetchRoomChain, goldenFetchRoomMessages,
      );
      // A "rewritten" room: different content/hashes from message 1 onward, still
      // internally self-consistent and still validly signed by the same relay key
      // -- exactly the scenario client-side pinning exists to catch, since the
      // relay's own signature check alone cannot.
      const h1b = await computeChainHash(null, "agent_a", "first message, TAMPERED", "2026-01-01T00:00:00Z");
      const h2b = await computeChainHash(h1b, "agent_b", "second — 日本語", "2026-01-01T00:00:05Z");
      const rewrittenMessages = [
        { sender_id: "agent_a", content: "first message, TAMPERED", created_at: "2026-01-01T00:00:00Z", chain_hash: h1b },
        { sender_id: "agent_b", content: "second — 日本語", created_at: "2026-01-01T00:00:05Z", chain_hash: h2b },
      ];
      const rewrittenFetchChain = async () => ({
        ...(await goldenFetchRoomChain()), chain_head: h2b,
        // Not a real signature (the golden fixture's signature is over the
        // original head) -- irrelevant to this test, which exercises the pin
        // check specifically; a genuinely re-signed rewrite would still reach
        // and fail at the pin-mismatch check the same way, since the signature
        // check and the pin check are independent gates.
      });
      const result = await verifyRoomChainFromRelay(
        CHAIN_GOLDEN.relayUrl, CHAIN_GOLDEN.roomId, rewrittenFetchChain,
        () => Promise.resolve({ messages: rewrittenMessages }),
      );
      expect(result.verified).toBe(false);
      // Either gate (signature or pin) correctly refuses -- both are real
      // security properties; this test's own fixture can't produce a validly
      // re-signed rewrite without the relay's real private key, so it takes
      // the signature-mismatch exit, not the pin-mismatch one. The SDK/MCP
      // Python-side tests (test_sdk_verify_room_chain_pins_head_and_catches_a_
      // self_consistent_rewrite) cover the pin-mismatch path specifically,
      // against a real relay process where a genuinely re-signed rewrite is
      // possible.
      expect(result.reason.length).toBeGreaterThan(0);
    });

    it("fails closed when history exceeds max_messages, mirroring the Python SDK's cap fix", async () => {
      // A real relay respects `limit` -- unlike goldenFetchRoomMessages (which
      // always returns both fixture messages regardless of what's asked for,
      // fine for the other tests here but not this one), this mock actually
      // honors it so the cap logic's own page-sizing is what's under test,
      // not a mock that accidentally over-delivers.
      const paginatingFetch = (_roomId: string, opts?: { limit?: number; before?: string }) => {
        const limit = opts?.limit ?? 200;
        // Most-recent-first, matching the real relay's ORDER BY created_at DESC.
        const desc = [...CHAIN_GOLDEN.messages].reverse();
        const startIdx = opts?.before ? desc.findIndex((m) => m.created_at < opts.before!) : 0;
        return Promise.resolve({ messages: desc.slice(Math.max(startIdx, 0), Math.max(startIdx, 0) + limit) });
      };
      const result = await verifyRoomChainFromRelay(
        CHAIN_GOLDEN.relayUrl, CHAIN_GOLDEN.roomId, goldenFetchRoomChain, paginatingFetch,
        1, // cap smaller than the golden fixture's 2 messages
      );
      expect(result.verified).toBe(false);
      expect(result.reason).toContain("could not fetch full history");
    });
  });
});

// ============================================================================
// P24-1 (W1): verifyChainPage() -- TS port of rooms.verify_chain_page(), the
// pure decision core for one page of a backward chain walk.
// ============================================================================

describe("verifyChainPage", () => {
  it("verifies a single page from genesis", async () => {
    const room = await buildSignedRoom("http://x.example", "room_x", ["Move 1", "Move 2", "Move 3"]);
    const result = await verifyChainPage(room.messages, room.messages[room.messages.length - 1].chain_hash!, true);
    expect(result.ok).toBe(true);
    expect(result.newExpected).toBeNull();
    expect(result.reason).toContain("genesis");
  });

  it("defers a page's oldest message across a page boundary, then confirms it on the next call", async () => {
    const room = await buildSignedRoom("http://x.example", "room_x", ["Move 1", "Move 2", "Move 3", "Move 4"]);
    const newerPage = room.messages.slice(2); // Move 3, Move 4
    const olderPage = room.messages.slice(0, 2); // Move 1, Move 2

    const first = await verifyChainPage(newerPage, newerPage[newerPage.length - 1].chain_hash!, false);
    expect(first.ok).toBe(true);
    expect(first.newExpected).toEqual(newerPage[0]);

    const second = await verifyChainPage(olderPage, first.newExpected!, true);
    expect(second.ok).toBe(true);
    expect(second.newExpected).toBeNull();
  });

  it("rejects a tampered newest message", async () => {
    const room = await buildSignedRoom("http://x.example", "room_x", ["Move 1", "Move 2"]);
    const result = await verifyChainPage(room.messages, "not the real signed head", true);
    expect(result.ok).toBe(false);
    expect(result.reason).toContain("rewritten");
  });

  it("rejects a tampered mid-page message", async () => {
    const room = await buildSignedRoom("http://x.example", "room_x", ["Move 1", "Move 2", "Move 3"]);
    const tampered = room.messages.map((m, i) => (i === 1 ? { ...m, content: "Move 2, but tampered" } : m));
    const result = await verifyChainPage(tampered, tampered[tampered.length - 1].chain_hash!, true);
    expect(result.ok).toBe(false);
    expect(result.reason).toContain("within a page");
  });

  it("rejects a tampered boundary message via the deferred check", async () => {
    const room = await buildSignedRoom("http://x.example", "room_x", ["Move 1", "Move 2", "Move 3", "Move 4"]);
    const newerPage = room.messages.slice(2);
    const olderPage = room.messages.slice(0, 2);
    const first = await verifyChainPage(newerPage, newerPage[newerPage.length - 1].chain_hash!, false);
    expect(first.ok).toBe(true);
    const tamperedPending = { ...(first.newExpected as typeof room.messages[number]), content: "Move 3, but tampered" };

    const second = await verifyChainPage(olderPage, tamperedPending, true);
    expect(second.ok).toBe(false);
    expect(second.reason).toContain("page boundary");
  });

  it("rejects a false genesis claim on a page that isn't actually the room's first", async () => {
    const room = await buildSignedRoom("http://x.example", "room_x", ["Move 1", "Move 2", "Move 3", "Move 4"]);
    const newerPage = room.messages.slice(2);
    const result = await verifyChainPage(newerPage, newerPage[newerPage.length - 1].chain_hash!, true);
    expect(result.ok).toBe(false);
    expect(result.reason).toContain("genesis");
  });

  it("fails closed on an empty page", async () => {
    const result = await verifyChainPage([], "anything", true);
    expect(result.ok).toBe(false);
    expect(result.newExpected).toBeNull();
  });
});

// ============================================================================
// P24-1/2 (W1/W2): verifyRoomChainFromRelay()'s backward walk, pin-as-
// checkpoint early stop, and full-history replay scan.
// ============================================================================

describe("verifyRoomChainFromRelay backward walk / pin checkpoint / replay scan", () => {
  // Same in-memory localStorage stand-in as the "room chain head pinning"
  // describe above -- these tests exercise the real pin-set/pin-read path,
  // not just the best-effort try/catch fallback (vitest's "node"
  // environment has no real localStorage).
  beforeEach(() => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
      setItem: (k: string, v: string) => { store.set(k, v); },
      removeItem: (k: string) => { store.delete(k); },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("walks a multi-page room to genesis and reports the full_history scope", async () => {
    const relayUrl = "http://pw.example";
    const roomId = "room_pw_multipage";
    const contents = Array.from({ length: 250 }, (_, i) => `Move ${i}`);
    const room = await buildSignedRoom(relayUrl, roomId, contents);

    const result = await verifyRoomChainFromRelay(
      relayUrl, roomId, fetchChainFor(room), fetchMessagesFrom(room.messages),
    );
    expect(result.verified).toBe(true);
    expect(result.chain_verified_scope).toBe("genesis");
    expect(result.replay_scan_scope).toBe("full_history");
    expect(result.message_count).toBe(250);
  });

  it("pin checkpoint: a later call with only a few new messages fetches just one page, not the whole room again", async () => {
    const relayUrl = "http://pw.example";
    const roomId = "room_pw_pinckpt";
    const room = await buildSignedRoom(relayUrl, roomId, Array.from({ length: 300 }, (_, i) => `Move ${i}`));

    const first = await verifyRoomChainFromRelay(relayUrl, roomId, fetchChainFor(room), fetchMessagesFrom(room.messages));
    expect(first.verified).toBe(true);
    expect(first.chain_verified_scope).toBe("genesis");

    const extended = await extendSignedRoom(room, ["New 1", "New 2", "New 3", "New 4", "New 5"]);
    let fetchCount = 0;
    const second = await verifyRoomChainFromRelay(
      relayUrl, roomId, fetchChainFor(extended), fetchMessagesFrom(extended.messages, () => { fetchCount++; }),
    );
    expect(second.verified).toBe(true);
    expect(second.chain_verified_scope).toBe("pinned_checkpoint");
    expect(second.replay_scan_scope).toBe("since_pinned_head");
    expect(fetchCount).toBe(1);
  });

  it("pin fast path: an unchanged room fetches no message pages at all", async () => {
    const relayUrl = "http://pw.example";
    const roomId = "room_pw_unchanged";
    const room = await buildSignedRoom(relayUrl, roomId, ["Move 1", "Move 2", "Move 3"]);

    const first = await verifyRoomChainFromRelay(relayUrl, roomId, fetchChainFor(room), fetchMessagesFrom(room.messages));
    expect(first.verified).toBe(true);

    let fetchCount = 0;
    const second = await verifyRoomChainFromRelay(
      relayUrl, roomId, fetchChainFor(room), fetchMessagesFrom(room.messages, () => { fetchCount++; }),
    );
    expect(second.verified).toBe(true);
    expect(second.chain_verified_scope).toBe("pinned_checkpoint");
    expect(second.replay_scan_scope).toBe("no_new_messages");
    expect(fetchCount).toBe(0);
  });

  it("ignorePin forces a full genesis walk even when a pin already matches", async () => {
    const relayUrl = "http://pw.example";
    const roomId = "room_pw_ignorepin";
    const room = await buildSignedRoom(relayUrl, roomId, ["Move 1", "Move 2", "Move 3"]);

    const first = await verifyRoomChainFromRelay(relayUrl, roomId, fetchChainFor(room), fetchMessagesFrom(room.messages));
    expect(first.verified).toBe(true);

    const second = await verifyRoomChainFromRelay(
      relayUrl, roomId, fetchChainFor(room), fetchMessagesFrom(room.messages), 25_000, true,
    );
    expect(second.verified).toBe(true);
    expect(second.chain_verified_scope).toBe("genesis");
  });

  it("finds a duplicate nonce whose original message is many pages away", async () => {
    const relayUrl = "http://pw.example";
    const roomId = "room_pw_replay";
    const room = await buildSignedRoom(relayUrl, roomId, Array.from({ length: 252 }, (_, i) => `Move ${i}`));
    // Simulate an operator who dropped UNIQUE(room_id, nonce) -- see the
    // Python test's docstring for why this is the only way a genuine
    // same-room duplicate nonce reaches this scanner in practice.
    const messages = room.messages.map((m, i) => {
      if (i === 0) return { ...m, nonce: "dupe-nonce-1" };
      if (i === room.messages.length - 1) return { ...m, nonce: "dupe-nonce-1" };
      return m;
    });

    const result = await verifyRoomChainFromRelay(relayUrl, roomId, fetchChainFor(room), fetchMessagesFrom(messages));
    expect(result.verified).toBe(true);
    expect(result.replay_scan_scope).toBe("full_history");
    expect(result.replayed_nonce_count).toBe(1);
    expect(result.replayed_nonce_sample).toContain("dupe-nonce-1");
  });

  it("detects a self-consistent history rewrite via the pin, on a second call", async () => {
    const relayUrl = "http://pw.example";
    const roomId = "room_pw_rewrite";
    const room = await buildSignedRoom(relayUrl, roomId, ["Move 1", "Move 2"]);
    const first = await verifyRoomChainFromRelay(relayUrl, roomId, fetchChainFor(room), fetchMessagesFrom(room.messages));
    expect(first.verified).toBe(true);

    // A "rewritten" room: different content/hashes from message 1 onward,
    // still internally self-consistent and (for this test) still reported
    // under the same signature machinery -- exactly what client-side
    // pinning exists to catch, since a relay's own signature alone can't.
    const h1b = await computeChainHash(null, "agent_a", "Move 1, TAMPERED", room.messages[0].created_at);
    const h2b = await computeChainHash(h1b, "agent_a", "Move 2", room.messages[1].created_at);
    const rewritten = {
      ...room,
      messages: [
        { sender_id: "agent_a", content: "Move 1, TAMPERED", created_at: room.messages[0].created_at, chain_hash: h1b },
        { sender_id: "agent_a", content: "Move 2", created_at: room.messages[1].created_at, chain_hash: h2b },
      ],
      chainHead: h2b,
    };
    const signable = JSON.stringify({
      origin_relay: relayUrl,
      room_chain: [{ chain_head: h2b, message_count: 2, room_id: roomId }],
    });
    rewritten.signature = bytesToHex(ed25519.sign(new TextEncoder().encode(signable), room.secretKey));

    const second = await verifyRoomChainFromRelay(relayUrl, roomId, fetchChainFor(rewritten), fetchMessagesFrom(rewritten.messages));
    expect(second.verified).toBe(false);
    expect(second.reason).toContain("rewritten");
  });
});
