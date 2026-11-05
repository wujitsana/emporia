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
