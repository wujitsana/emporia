import { describe, expect, it } from "vitest";
import { verifyMessageSignature } from "./integrityCheck";
import { keyAt, keyLogWindows, senderKeyFrom, whenOf } from "./keyLog";
// Made by scripts/keylog_vectors.py, i.e. by the Python side (identity.sign_payload, wire.key_log_windows, identity.verify_dm_message):
// a rotation, a recovery commitment, a thief's rotation and a recovery that undoes it, and a DM
// signed in each key's time, with Python's own verdict on each.
import vectors from "./keyLog.vectors.json";

describe("an agent's key log, read as the Python side reads it (P253)", () => {
  it("gives the same windows as wire.key_log_windows", () => {
    expect(keyLogWindows(vectors.stable_id, vectors.key_log, vectors.current)).toEqual(vectors.windows);
  });

  it("checks each message against the key its sender had then, as identity.verify_dm_message", () => {
    const sender = senderKeyFrom({ public_key_hex: vectors.current, stable_id: vectors.stable_id, key_log: vectors.key_log });
    for (const m of vectors.messages) {
      expect([m.content, verifyMessageSignature("dm", "dm_t1", m, sender)]).toEqual([m.content, m.python_verified]);
    }
  });

  it("checked against today's key alone, everything signed before the rotation fails", () => {
    const before = vectors.messages[0];
    expect(verifyMessageSignature("dm", "dm_t1", before, vectors.current)).toBe(false);
  });

  it("refuses a log that is not the agent's, or was changed", () => {
    const steps = vectors.key_log;
    expect(keyLogWindows("00".repeat(32), steps, vectors.current)).toEqual([]);
    const forged = steps.map((s, i) => (i === 0 ? { ...s, new: steps[2].new } : s));
    expect(keyLogWindows(vectors.stable_id, forged, vectors.current)).toEqual([]);
    const reordered = [steps[1], steps[0], ...steps.slice(2)];
    expect(keyLogWindows(vectors.stable_id, reordered, vectors.current)).toEqual([]);
    // an invalid log keeps no windows: only today's key, so old messages fail rather than pass
    const sender = senderKeyFrom({ public_key_hex: vectors.current, stable_id: vectors.stable_id, key_log: forged });
    expect(sender.windows).toEqual([]);
    expect(verifyMessageSignature("dm", "dm_t1", vectors.messages[0], sender)).toBe(false);
  });

  it("keeps no windows when the log ends at a key other than the one served", () => {
    const sender = senderKeyFrom({ public_key_hex: vectors.windows[0].key, stable_id: vectors.stable_id, key_log: vectors.key_log });
    expect(sender.windows).toEqual([]);
  });

  it("reads times as Python does: no zone is UTC, microseconds count", () => {
    expect(whenOf("2026-10-01T10:00:00")).toBe(whenOf("2026-10-01T10:00:00+00:00"));
    expect(whenOf("2026-10-01T10:00:00Z")).toBe(whenOf("2026-10-01T12:00:00+02:00"));
    expect(whenOf("2026-10-01T10:00:00.000002")! - whenOf("2026-10-01T10:00:00.000001")!).toBe(1);
    expect(whenOf("not a time")).toBeNull();
    expect(keyAt(vectors.windows, "")).toBe(vectors.current);
    expect(keyAt(vectors.windows, "1999-01-01T00:00:00")).toBe(vectors.windows[0].key);
  });
});
