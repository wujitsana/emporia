// @vitest-environment jsdom
/** The header names the relay (its public host, or "local relay"), never the Hermes dashboard's address, never a guardrail mode. */
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { Health } from "./api";
import { RelayStrip } from "./RelayStrip";
import { relayLabel } from "./relayLabel";

afterEach(cleanup);

const base: Health = { status: "ok", service: "emporia", version: "1.2.3", modules: [], session_count: 0 };

describe("relayLabel", () => {
  it("is the relay's public host from /health", () => {
    expect(relayLabel({ relay_url: "https://emporia.wujitsana.net", host: "emporia.wujitsana.net" })).toBe("emporia.wujitsana.net");
    expect(relayLabel({ host: "emporia.wujitsana.net" })).toBe("emporia.wujitsana.net");
    expect(relayLabel({ relay_url: "https://Relay.Example.com:8443/x" })).toBe("relay.example.com");
  });

  it("is 'local relay' for localhost and 127.x, with or without a port", () => {
    for (const relay_url of ["http://localhost:8088", "http://127.0.0.1:8088", "http://127.1.2.3", "http://[::1]:8088", "http://0.0.0.0:1"]) {
      expect(relayLabel({ relay_url })).toBe("local relay");
    }
    expect(relayLabel({ host: "localhost:8088" })).toBe("local relay");
  });

  it("is plain 'relay' while the relay has not said who it is", () => {
    expect(relayLabel(null)).toBe("relay");
    expect(relayLabel({})).toBe("relay");
  });
});

describe("the header's relay chip", () => {
  const strip = (health: Health | null, relayUrl = "http://localhost:9119", compact = false) =>
    render(<RelayStrip relayUrl={relayUrl} wsConnected={false} health={health} compact={compact} />).container;

  it("names the relay and shows the network chip, never the page's own address (the Hermes dashboard's)", () => {
    for (const compact of [false, true]) {
      const c = strip({ ...base, relay_url: "https://emporia.wujitsana.net", host: "emporia.wujitsana.net", x402_escrow_network: "eip155:84532" }, "http://localhost:9119", compact);
      expect(c.textContent).toContain("emporia.wujitsana.net");
      expect(c.textContent).toContain("testnet");
      expect(c.textContent).not.toMatch(/9119|localhost/);
      expect(c.innerHTML).not.toMatch(/9119/);
      cleanup();
    }
  });

  it("says 'local relay' for a relay on this machine", () => {
    const c = strip({ ...base, relay_url: "http://localhost:8088", host: "localhost:8088" }, "http://localhost:9119");
    expect(c.querySelector(".e-status-chip__label--relay")?.textContent).toBe("local relay");
    expect(c.innerHTML).not.toMatch(/9119/);
  });

  it("carries no guardrail mode", () => {
    const c = strip({ ...base, guardrails_mode: "enforce", relay_url: "https://emporia.wujitsana.net" });
    expect(c.textContent).not.toMatch(/enforce|guardrail/i);
  });
});
