import { describe, expect, it, vi } from "vitest";
import { resolveViewerId } from "./relayEnv";

// P35-1: the dashboard read VITE_AGENT_ID in four places with three different
// answers for "unset":
//   relayEnv.ts      `?? "dashboard"`  -> a truthy placeholder
//   relayContext.tsx `?? null`         -> isSpectator
//   SocialHubView    `?? "dashboard"`  -> its own private duplicate
//   api.ts           no fallback       -> header omitted
// The placeholder is what made this dangerous rather than merely untidy: every
// `if (!VIEWER)` guard in the app tested the falsy branch of a value that was
// never falsy, so all five were dead -- including the user-facing
// "Set VITE_AGENT_ID to view your inbox" message in MessagesView, which could
// not render. Meanwhile four other call sites had noticed the placeholder was
// lying and re-derived identity as `VIEWER !== "dashboard"`, so the codebase
// carried two competing idioms for one question and the shorter one was dead.

describe("resolveViewerId", () => {
  it("returns null when VITE_AGENT_ID is unset -- the falsy guards must fire", () => {
    expect(resolveViewerId(undefined)).toBeNull();
  });

  it("treats empty and whitespace-only as unset", () => {
    expect(resolveViewerId("")).toBeNull();
    expect(resolveViewerId("   ")).toBeNull();
  });

  it("passes a real agent id through, trimmed", () => {
    expect(resolveViewerId("hackathon_hermes")).toBe("hackathon_hermes");
    expect(resolveViewerId("  hackathon_hermes  ")).toBe("hackathon_hermes");
  });

  it("no longer treats 'dashboard' as a sentinel -- it is just an agent id", () => {
    // The old code had it both ways: relayContext saw the string and reported
    // isSpectator=false (nav shows Messages/Fees), while AgentsView and
    // AgorasView string-compared it away and reported no identity. Anyone
    // following .env.example got exactly that split state.
    expect(resolveViewerId("dashboard")).toBe("dashboard");
  });
});

describe("VIEWER_ID module constant", () => {
  it("is null when the env var is absent", async () => {
    vi.stubEnv("VITE_AGENT_ID", undefined as unknown as string);
    vi.resetModules();
    const m = await import("./relayEnv");
    expect(m.VIEWER_ID).toBeNull();
    // The property every `if (!VIEWER_ID) return;` in the app depends on.
    expect(!m.VIEWER_ID).toBe(true);
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("carries the configured id when the env var is set", async () => {
    vi.stubEnv("VITE_AGENT_ID", "some_agent");
    vi.resetModules();
    const m = await import("./relayEnv");
    expect(m.VIEWER_ID).toBe("some_agent");
    vi.unstubAllEnvs();
    vi.resetModules();
  });
});
