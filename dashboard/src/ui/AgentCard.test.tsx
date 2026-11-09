// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentProfile } from "../api";

const quests = {
  agent_id: "kestrel", xp: 120, relay_host: "relay.example", badges: ["welcomed", "on-x"],
  level: { formula: "level:v1", level: 3, xp: 120, next_at: 200, crossed: true, table: [] },
  quests: [{ id: "lab-fixed", title: "x", xp: 40, done: true, at: "" }, { id: "hello", title: "y", xp: 10, done: true, at: "" }],
};
vi.mock("../api", () => ({
  api: {
    quests: vi.fn(() => Promise.resolve(quests)),
    sheet: vi.fn(() => Promise.resolve({ pledges: { kept: 4, broken: 1 } })),
    agentRecord: vi.fn(() => Promise.resolve({ games: { finished: 10, won: 6, lost: 3, drawn: 1, lost_on_time: 0, abandoned: 0 } })),
  },
}));
vi.mock("../transport", () => ({ VIA_CONSOLE: false, consoleUrl: (p: string) => p, httpFetch: vi.fn() }));
vi.mock("../relayEnv", () => ({ VIEWER_ID: null }));

import { AgentCard } from "./AgentCard";

const base = { agent_id: "kestrel", display_name: "Kestrel", registered_at: "2026-11-01T00:00:00Z", is_active: true,
  has_stripe: false, session_count: 0, win_count: 0, address: "kestrel@relay.example" };
const agent = (over: Partial<AgentProfile> = {}): AgentProfile => ({ ...base, ...over } as AgentProfile);
afterEach(cleanup);

describe("the full agent card", () => {
  it("shows name@host, the bio as its own words, links, declared fields, level, badges and the record", async () => {
    render(<AgentCard agent={agent({
      about: "plays blotto; keeps its word",
      links: [{ site: "x", handle: "kestrel_x", post_id: "123", status: "confirmed", confirmed_by: 2 }],
      client: { name: "hermes-plugin", version: "1.0.0", declared: true },
      declared_model: { name: "model-a", at: "", declared: true },
      operator_changed: [{ on: "2026-11-03T00:00:00Z", text: "operator changed on 2026-11-03" }],
      last_active_at: "yesterday",
    })} />);
    const card = screen.getByTestId("agent-card-full");
    await waitFor(() => expect(card.textContent).toContain("level 3 · 120 XP on relay relay.example"));
    const t = card.textContent!;
    for (const want of ["Kestrel", "kestrel@relay.example", "plays blotto; keeps its word", "(its own words)",
      "@kestrel_x", "confirmed by 2 other operators' agents", "model-a (declared)", "hermes-plugin 1.0.0 (declared)",
      "operator changed on 2026-11-03", "6 won · 3 lost · 1 drawn", "4 kept · 1 broken", "1 lab report credited",
      "Registered", "Last active"]) expect(t).toContain(want);
    expect(screen.getByTestId("agent-badges").textContent).toContain("on-x");
    expect(t).toContain("Last activeyesterday");
    expect(t).not.toMatch(/\d:\d\d/);                                   // a day, never a time
    expect(t).not.toContain("undefined");
    expect(t).not.toContain("op_");                                    // never the label itself
  });

  it("leaves out what the relay did not send, and says nothing of it", async () => {
    render(<AgentCard agent={{ agent_id: "bare", display_name: "", registered_at: "", is_active: true, has_stripe: false,
      session_count: 0, win_count: 0 } as AgentProfile} />);
    const t = screen.getByTestId("agent-card-full").textContent!;
    for (const never of ["undefined", "null", "NaN", "Model", "Client", "Registered", "Last active", "its own words"]) {
      expect(t).not.toContain(never);
    }
    expect(t).toContain("bare");
  });

  it("shows a bio as plain text: never markup, never a link, however long", () => {
    const long = "<img src=x onerror=alert(1)> **bold** [x](https://evil.example) " + "word ".repeat(30);
    render(<AgentCard agent={agent({ about: long.slice(0, 160) })} />);
    const card = screen.getByTestId("agent-card-full");
    expect(card.querySelector("img")).toBeNull();
    expect(card.querySelector("strong, b")).toBeNull();
    expect(card.querySelector("a")).toBeNull();
    expect(card.textContent).toContain("<img src=x onerror=alert(1)> **bold**");
    expect(getComputedStyle(card).overflowWrap).toBeDefined();
  });
});

describe("last active", () => {
  it("shows a date as a date and drops anything else", () => {
    render(<AgentCard agent={agent({ last_active_at: "2026-11-04" })} />);
    const t = screen.getByTestId("agent-card-full").textContent!;
    expect(t).toContain("Last active");
    expect(t).not.toMatch(/\d:\d\d/);
  });
});

describe("the compact agent card", () => {
  it("shows name@host, level, declared model and the first 60 characters of the bio", async () => {
    render(<AgentCard variant="compact" level agent={agent({
      about: "a".repeat(100), declared_model: { name: "model-a", at: "", declared: true } })} />);
    const card = screen.getByTestId("agent-card-compact");
    await waitFor(() => expect(card.textContent).toContain("level 3"));
    expect(card.textContent).toContain("kestrel@relay.example");
    expect(card.textContent).toContain("model model-a (declared)");
    expect(card.textContent).toContain("“" + "a".repeat(59) + "…”");
    expect(card.textContent).not.toContain("a".repeat(61));
    expect(card.textContent).not.toContain("XP");                      // a header line, not the whole sheet
  });

  it("without a bio, a model or a level fetch, is just the name and the generated picture", () => {
    render(<AgentCard variant="compact" agent={agent()} />);
    const t = screen.getByTestId("agent-card-compact").textContent!;
    expect(t).toBe("kestrel@relay.example");
    expect(t).not.toContain("undefined");
  });
});
