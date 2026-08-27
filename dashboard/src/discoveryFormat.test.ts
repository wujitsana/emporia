import { describe, expect, it } from "vitest";
import { expiryLabel } from "./discoveryFormat";

// P40-1: `origin_relay` on a lobby challenge comes from a federated peer — a
// value from outside this relay's control, rendered directly in a table cell.
// `new URL()` throws on anything that isn't an absolute URL, and a throw during
// render takes down the whole Listings view, not just the cell.

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
