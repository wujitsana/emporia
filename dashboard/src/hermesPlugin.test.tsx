// @vitest-environment jsdom
/**
 * The Hermes tab across a profile switch: Hermes drops the old profile's bundle, loads the new
 * profile's (a fresh script run, tagged data-hermes-profile) and mounts its registered component
 * on a new element. The new tab must draw the new profile's app without a browser refresh.
 */
import { act, cleanup, render } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

class NoSocket {
  static OPEN = 1;
  readyState = 3;
  onclose: (() => void) | null = null;
  constructor() { setTimeout(() => this.onclose?.(), 0); }
  send() {}
  close() {}
}

const roots: ShadowRoot[] = [];
const realAttach = Element.prototype.attachShadow;

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }));
}

/** Runs the bundle the way Hermes does for one profile and returns the component it registers. */
async function loadBundle(profile: string): Promise<React.ComponentType> {
  vi.resetModules();
  const script = document.createElement("script");
  script.setAttribute("data-hermes-profile", profile);
  script.setAttribute("src", `http://localhost/dashboard-plugins/@${profile}/emporia/dist/index.js`);
  Object.defineProperty(document, "currentScript", { value: script, configurable: true });
  let registered: React.ComponentType | null = null;
  const w = window as unknown as Record<string, unknown>;
  w.__HERMES_PLUGIN_SDK__ = {
    React,
    hooks: { useRef: React.useRef, useEffect: React.useEffect },
    authedFetch: (url: string) => String(url).endsWith("/agents") ? json({ agents: ["alpha", "beta"], sockets: false }) : json({}),
    buildWsUrl: async () => "ws://x",
  };
  w.__HERMES_PLUGINS__ = { register: (_n: string, c: unknown) => { registered = c as React.ComponentType; } };
  await import("./hermesPlugin");
  Object.defineProperty(document, "currentScript", { value: null, configurable: true });
  if (!registered) throw new Error("bundle did not register");
  return registered;
}

async function settle() {
  for (let i = 0; i < 600 && !(roots.at(-1)?.textContent ?? "").trim(); i++) await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
}

beforeEach(() => {
  roots.length = 0;
  Element.prototype.attachShadow = function (init: ShadowRootInit) {
    const r = realAttach.call(this, { ...init, mode: "open" });
    roots.push(r);
    return r;
  };
  vi.stubGlobal("fetch", vi.fn((url: unknown) => String(url).includes("style.css")
    ? Promise.resolve(new Response("", { status: 200 }))
    : String(url).includes("/health") ? json({ status: "ok" }) : Promise.reject(new Error("offline"))));
  vi.stubGlobal("WebSocket", NoSocket);
  Element.prototype.scrollIntoView = () => {};
  window.matchMedia = (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  Element.prototype.attachShadow = realAttach;
  vi.unstubAllGlobals();
});

describe("the Hermes tab", () => {
  it("draws the app, and again for the next profile after a switch (no refresh)", async () => {
    const A = await loadBundle("alpha");
    const first = render(React.createElement(A));
    await settle();
    expect(roots.at(-1)!.textContent!.length, "alpha's tab is blank").toBeGreaterThan(20);
    first.unmount();

    const B = await loadBundle("beta");
    render(React.createElement(B));
    await settle();
    const text = roots.at(-1)!.textContent ?? "";
    expect(text.length, "beta's tab is blank after the switch").toBeGreaterThan(20);
    expect((window as unknown as { __EMPORIA_HERMES_PROFILE__?: { name: string } }).__EMPORIA_HERMES_PROFILE__?.name).toBe("beta");
  }, 60_000);

  it("a tab dropped while it was still starting leaves nothing behind for the next profile", async () => {
    const A = await loadBundle("alpha");
    render(React.createElement(A)).unmount();   // Hermes switched before alpha's tab finished mounting
    const B = await loadBundle("beta");
    render(React.createElement(B));
    await settle();
    await act(async () => { await new Promise((r) => setTimeout(r, 300)); });   // alpha's late steps would land now
    expect(roots.at(-1)!.textContent!.length, "beta's tab is blank").toBeGreaterThan(20);
    expect((window as unknown as { __EMPORIA_HERMES_PROFILE__?: { name: string } }).__EMPORIA_HERMES_PROFILE__?.name).toBe("beta");
  }, 60_000);
});
