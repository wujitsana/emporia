// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { isNotRegistered } from "./notRegistered";
import { setViewer } from "./relayEnv";
import { RelayContextProvider } from "./relayContext";
import { NotOnRelayNote } from "./ui/NotOnRelayNote";

describe("isNotRegistered", () => {
  it("is a 401 and nothing else", () => {
    expect(isNotRegistered(new Error("401 /agents/alpha"))).toBe(true);
    expect(isNotRegistered(new Error("403 /agents/alpha"))).toBe(false);
    expect(isNotRegistered(new Error("500 /agents/alpha"))).toBe(false);
    expect(isNotRegistered(new TypeError("Failed to fetch"))).toBe(false);
    expect(isNotRegistered(undefined)).toBe(false);
  });
});

describe("the not-on-this-relay note", () => {
  beforeEach(() => {
    setViewer("alpha");
    vi.spyOn(api, "uiConfig").mockResolvedValue({ relay_url: "https://relay.example:8443" } as never);
  });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); setViewer(null); });

  const shown = () => render(<RelayContextProvider><NotOnRelayNote /></RelayContextProvider>);

  it("names the agent and the relay and says how to join, on a 401", async () => {
    vi.spyOn(api, "agentProfile").mockRejectedValue(new Error("401 /agents/alpha"));
    shown();
    const note = await screen.findByRole("note");
    expect(note.textContent).toBe("alpha is not on relay.example:8443 yet. Run hermes emporia setup to join.");
  });

  it.each([["403 /agents/alpha"], ["Failed to fetch"], ["500 /agents/alpha"]])("says nothing on %s", async (msg) => {
    const spy = vi.spyOn(api, "agentProfile").mockRejectedValue(new Error(msg));
    shown();
    await waitFor(() => expect(spy).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("says nothing for a registered agent, and asks once", async () => {
    const spy = vi.spyOn(api, "agentProfile").mockResolvedValue({} as never);
    shown();
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("note")).toBeNull();
  });
});
