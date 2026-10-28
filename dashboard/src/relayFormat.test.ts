import { describe, expect, it } from "vitest";
import { relayHost, whenText } from "./relayFormat";

// P42: one relayHost, not two. RelayStrip.tsx had one (for federation peers) and
// P40 added a second in discoveryFormat.ts (for a lobby challenge's
// origin_relay) with different fallbacks — 48-char truncation vs 24, and a
// "local" default baked into URL formatting. Consolidated; "local" moved to the
// caller that actually has a notion of local.

describe("relayHost", () => {
  it("shows the host of an absolute URL, port included", () => {
    expect(relayHost("https://peer.example.com:8443/relay")).toBe("peer.example.com:8443");
    expect(relayHost("http://127.0.0.1:8088")).toBe("127.0.0.1:8088");
  });

  it("is empty for a missing value, so the caller can name its own default", () => {
    expect(relayHost(null)).toBe("");
    expect(relayHost(undefined)).toBe("");
    expect(relayHost("")).toBe("");
  });

  it("degrades to a label rather than throwing on a malformed origin", () => {
    // A federated peer is outside this relay's control, and a throw during
    // render takes down the whole view rather than one cell.
    expect(() => relayHost("not a url")).not.toThrow();
    expect(relayHost("not a url")).toBe("not a url");
  });

  it("strips the protocol and truncates an unparseable origin", () => {
    expect(relayHost(`https://${"x".repeat(200)}`)).toHaveLength(48);
  });
});

describe("whenText (P228)", () => {
  const now = new Date(2026, 9, 28, 12, 0);
  it("is the time alone for today", () => {
    expect(whenText(new Date(2026, 9, 28, 9, 5).toISOString(), now)).not.toMatch(/Oct/);
  });
  it("adds the date for an earlier day", () => {
    expect(whenText(new Date(2026, 9, 23, 21, 4).toISOString(), now)).toMatch(/23/);
  });
  it("is empty for nothing", () => {
    expect(whenText(undefined, now)).toBe("");
    expect(whenText("not a date", now)).toBe("");
  });
});
