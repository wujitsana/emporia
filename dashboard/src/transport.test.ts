// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

/*
 * The tab's relay sockets, where Hermes cannot carry them. A profile that is not the one the
 * dashboard was launched as runs the plugin in its plugin host; Hermes answers each socket to it
 * with an HTTP 501, and uvicorn logs every one as "ASGI callable returned without completing
 * handshake" — once per socket and per retry, in the user's terminal. The plugin says so
 * (`sockets: false`), and the tab then dials nothing.
 */

type Sdk = {
  authedFetch: (url: string) => Promise<Response>;
  buildWsUrl: (path: string, params?: Record<string, string>) => Promise<string>;
  sockets?: boolean;
};

async function loadWith(sdk: Sdk) {
  vi.resetModules();
  (window as { __EMPORIA_HERMES_SDK__?: Sdk }).__EMPORIA_HERMES_SDK__ = sdk;
  return import("./transport");
}

afterEach(() => {
  delete (window as { __EMPORIA_HERMES_SDK__?: Sdk }).__EMPORIA_HERMES_SDK__;
  vi.unstubAllGlobals();
});

/** A WebSocket stand-in whose open and close the test fires. */
class FakeWs {
  static made: FakeWs[] = [];
  listeners: Record<string, (() => void)[]> = {};
  url: string;
  constructor(url: string) {
    this.url = url;
    FakeWs.made.push(this);
  }
  addEventListener(type: string, fn: () => void) {
    (this.listeners[type] ??= []).push(fn);
  }
  fire(type: string) {
    (this.listeners[type] ?? []).forEach((fn) => fn());
  }
}

describe("a plugin that does not say (older, or refusing for another reason)", () => {
  it("stops dialling after a few refusals with none ever open", async () => {
    FakeWs.made = [];
    vi.stubGlobal("WebSocket", FakeWs);
    const t = await loadWith({ authedFetch: vi.fn(), buildWsUrl: async () => "ws://x/" });
    t._resetSocketRefusals();
    for (let i = 0; i < t.REFUSALS_BEFORE_QUIET; i++) {
      const s = (await t.relaySocket("/ws/events")) as unknown as FakeWs;
      s.fire("close");
    }
    expect(await t.relaySocket("/ws/events")).toBeInstanceOf(t.QuietSocket);
    expect(FakeWs.made).toHaveLength(t.REFUSALS_BEFORE_QUIET);
  });

  it("keeps dialling once a socket has opened (a dropped one is not a refusal)", async () => {
    FakeWs.made = [];
    vi.stubGlobal("WebSocket", FakeWs);
    const t = await loadWith({ authedFetch: vi.fn(), buildWsUrl: async () => "ws://x/" });
    t._resetSocketRefusals();
    const first = (await t.relaySocket("/ws/events")) as unknown as FakeWs;
    first.fire("open");
    first.fire("close");
    for (let i = 0; i < t.REFUSALS_BEFORE_QUIET + 1; i++) ((await t.relaySocket("/ws/events")) as unknown as FakeWs).fire("close");
    expect(await t.relaySocket("/ws/events")).toBeInstanceOf(FakeWs);
  });
});

describe("relay sockets in the Hermes tab", () => {
  it("dials none when the plugin host carries no sockets, and the quiet socket never retries", async () => {
    const buildWsUrl = vi.fn(async () => "ws://x/");
    const Ws = vi.fn();
    vi.stubGlobal("WebSocket", Ws);
    const t = await loadWith({ authedFetch: vi.fn(), buildWsUrl, sockets: false });
    expect(t.MODE).toBe("hermes");
    expect(t.socketsCarried()).toBe(false);
    const s = await t.relaySocket("/ws/events");
    expect(s).toBeInstanceOf(t.QuietSocket);
    expect(buildWsUrl).not.toHaveBeenCalled();
    expect(Ws).not.toHaveBeenCalled();
    // A hook's onclose would schedule a redial: the quiet socket never fires one.
    const onclose = vi.fn();
    s.onclose = onclose;
    s.send("{}");
    s.close();
    expect(onclose).not.toHaveBeenCalled();
    expect(s.readyState).toBe(3);
  });

  it("dials through Hermes's ticketed URL where sockets are carried (and when the plugin does not say)", async () => {
    for (const sockets of [true, undefined]) {
      const buildWsUrl = vi.fn(async () => "ws://host/api/plugins/emporia/ws/x?ticket=t");
      const Ws = vi.fn();
      vi.stubGlobal("WebSocket", Ws);
      const t = await loadWith({ authedFetch: vi.fn(), buildWsUrl, sockets });
      expect(t.socketsCarried()).toBe(true);
      await t.relaySocket("/ws/events");
      expect(buildWsUrl).toHaveBeenCalledOnce();
      expect(Ws).toHaveBeenCalledWith("ws://host/api/plugins/emporia/ws/x?ticket=t");
    }
  });
});
