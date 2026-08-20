import { describe, expect, it } from "vitest";
import {
  ENCRYPTED_PLACEHOLDER,
  dmDisplayContent,
  dmThreadPreview,
  looksLikeEncryptedEnvelope,
  type DMThread,
} from "./api";

// P21-6: the JS-side half of D2 -- these three functions are the whole reason
// the dashboard didn't catch P19's own regression (a stale `v === 1` check
// after crypto_box.ENVELOPE_VERSION bumped to 2), because dashboard/ had no
// test runner at all before this pass. The cross-language pin
// (tests/test_emporia.py's test_dashboard_envelope_check_does_not_hardcode_a_
// stale_version, which greps this file's source text) stays as the guard
// against a *hardcoded* version; these tests exercise the actual running
// logic instead, including cases the text-grep can't see (malformed JSON,
// missing fields, the DMThread encrypted-flag short-circuit).

describe("looksLikeEncryptedEnvelope", () => {
  it("recognizes a well-formed envelope", () => {
    const env = JSON.stringify({ v: 2, alg: "x25519-xsalsa20-poly1305", keys: { a: "x" } });
    expect(looksLikeEncryptedEnvelope(env)).toBe(true);
  });

  it("accepts any version >= 1, not just the current one -- the P19/P20-1 regression", () => {
    // The exact class of bug this pass exists to prevent: a hardcoded `v === N`
    // check silently diverges the moment crypto_box.ENVELOPE_VERSION bumps.
    for (const v of [1, 2, 3, 99]) {
      const env = JSON.stringify({ v, alg: "x25519-xsalsa20-poly1305", keys: { a: "x" } });
      expect(looksLikeEncryptedEnvelope(env)).toBe(true);
    }
  });

  it("rejects a version below the floor", () => {
    const env = JSON.stringify({ v: 0, alg: "x25519-xsalsa20-poly1305", keys: { a: "x" } });
    expect(looksLikeEncryptedEnvelope(env)).toBe(false);
  });

  it("rejects plain chat text", () => {
    expect(looksLikeEncryptedEnvelope("hey, want to grab coffee?")).toBe(false);
  });

  it("rejects malformed JSON without throwing", () => {
    expect(looksLikeEncryptedEnvelope("{not valid json")).toBe(false);
  });

  it("rejects a JSON object missing required envelope fields", () => {
    expect(looksLikeEncryptedEnvelope(JSON.stringify({ v: 2 }))).toBe(false);
    expect(looksLikeEncryptedEnvelope(JSON.stringify({ alg: "x", keys: {} }))).toBe(false);
    expect(looksLikeEncryptedEnvelope(JSON.stringify({ v: 2, alg: "x" }))).toBe(false);
  });

  it("rejects null, undefined, and empty string", () => {
    expect(looksLikeEncryptedEnvelope(null)).toBe(false);
    expect(looksLikeEncryptedEnvelope(undefined)).toBe(false);
    expect(looksLikeEncryptedEnvelope("")).toBe(false);
  });

  it("rejects a JSON value that isn't an object, even if it starts with '{'", () => {
    // content[0] !== '{' short-circuits most non-objects; this exercises the
    // remaining case where JSON.parse succeeds but the shape is still wrong.
    expect(looksLikeEncryptedEnvelope('{"just": "a plain dict, no envelope fields"}')).toBe(false);
  });
});

describe("dmDisplayContent", () => {
  it("shows the lock placeholder for an envelope, never the raw JSON", () => {
    const env = JSON.stringify({ v: 2, alg: "x25519-xsalsa20-poly1305", keys: { a: "x" } });
    expect(dmDisplayContent(env)).toBe(ENCRYPTED_PLACEHOLDER);
  });

  it("passes plaintext through unchanged", () => {
    expect(dmDisplayContent("plain text message")).toBe("plain text message");
  });
});

describe("dmThreadPreview", () => {
  const base: DMThread = {
    thread_id: "dm_1", other_agent: "bob", last_message_at: "2026-01-01T00:00:00Z",
    last_content: null, last_sender: null,
  };

  it("shows the lock when the thread is flagged encrypted, even with plaintext-looking content", () => {
    // thread.encrypted is checked first specifically so a thread that upgraded
    // to encryption mid-conversation doesn't leak a stale plaintext preview.
    expect(dmThreadPreview({ ...base, encrypted: true, last_content: "not json at all" }))
      .toBe("🔒 encrypted");
  });

  it("shows the lock when last_content looks like an envelope, even if encrypted is unset", () => {
    const env = JSON.stringify({ v: 2, alg: "x", keys: { a: "x" } });
    expect(dmThreadPreview({ ...base, last_content: env })).toBe("🔒 encrypted");
  });

  it("shows a truncated plaintext preview otherwise", () => {
    expect(dmThreadPreview({ ...base, last_content: "hello there, how's it going?" }))
      .toBe("hello there, how's it going?");
  });

  it("truncates to 36 characters", () => {
    const long = "x".repeat(50);
    expect(dmThreadPreview({ ...base, last_content: long })).toBe("x".repeat(36));
  });

  it("shows an em-dash placeholder for a thread with no messages yet", () => {
    expect(dmThreadPreview(base)).toBe("—");
  });
});
