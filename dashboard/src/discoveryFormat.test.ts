import { describe, expect, it } from "vitest";
import { expiryLabel, relayHost } from "./discoveryFormat";

// P40-1: `origin_relay` on a lobby challenge comes from a federated peer — a
// value from outside this relay's control, rendered directly in a table cell.
// `new URL()` throws on anything that isn't an absolute URL, and a throw during
// render takes down the whole Listings view, not just the cell.

describe("relayHost", () => {
  it("shows the host of a federated peer", () => {
    expect(relayHost("https://peer.example.com:8443/relay")).toBe("peer.example.com:8443");
  });

  it("calls a challenge that originated here local", () => {
    expect(relayHost(null)).toBe("local");
    expect(relayHost(undefined)).toBe("local");
    expect(relayHost("")).toBe("local");
  });

  it("degrades to a label rather than throwing on a malformed origin", () => {
    // The whole point: a peer sending junk must not blank the view.
    expect(() => relayHost("not a url")).not.toThrow();
    expect(relayHost("not a url")).toBe("not a url");
  });

  it("truncates an absurdly long unparseable origin", () => {
    const junk = "x".repeat(500);
    expect(relayHost(junk)).toHaveLength(24);
  });
});

describe("expiryLabel", () => {
  const now = Date.parse("2026-01-01T00:00:00Z");

  it("shows hours inside two days", () => {
    expect(expiryLabel("2026-01-01T03:00:00Z", now)).toBe("3h");
    expect(expiryLabel("2026-01-02T23:00:00Z", now)).toBe("47h");
  });

  it("switches to days at 48 hours", () => {
    expect(expiryLabel("2026-01-03T00:00:00Z", now)).toBe("2d");
  });

  it("says expired rather than rendering a negative", () => {
    expect(expiryLabel("2025-12-31T23:00:00Z", now)).toBe("expired");
  });

  it("is blank for no expiry and for an unparseable one", () => {
    expect(expiryLabel(null, now)).toBe("");
    expect(expiryLabel("whenever", now)).toBe("");
  });
});
