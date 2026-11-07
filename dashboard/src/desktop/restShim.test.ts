import { describe, expect, it, vi } from "vitest";
import type { DesktopBridge } from "./bridge";
import { PluginSocket, restFetch, statusOf } from "./restShim";

function bridge(rest: DesktopBridge["rest"], socket: DesktopBridge["socket"] = () => () => {}): DesktopBridge {
  return { rest, socket };
}

/** What Electron's main process throws for an HTTP error (httpStatusError), as the renderer sees it. */
function httpError(status: number, body: string, viaIpc = false): Error {
  const e = new Error(`${viaIpc ? "Error invoking remote method 'hermes:api': Error: " : ""}${status}: ${body}`);
  if (!viaIpc) (e as Error & { statusCode: number }).statusCode = status;
  return e;
}

describe("restFetch: fetch over ctx.rest", () => {
  it("asks ctx.rest for the path inside the plugin's namespace and answers a JSON Response", async () => {
    const rest = vi.fn(async () => ({ agents: ["alpha"] }));
    const r = await restFetch(bridge(rest as DesktopBridge["rest"]), "/api/plugins/emporia/relay/alpha/sessions?status=active");
    expect(rest).toHaveBeenCalledWith("/relay/alpha/sessions?status=active", { method: "GET" });
    expect(r.status).toBe(200);
    expect(r.ok).toBe(true);
    expect(await r.json()).toEqual({ agents: ["alpha"] });
  });

  it("sends a JSON body as the object ctx.rest serialises itself", async () => {
    const rest = vi.fn(async () => ({ opened: {} }));
    await restFetch(bridge(rest as DesktopBridge["rest"]), "/api/plugins/emporia/dm/alpha/open", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ thread_id: "t1", messages: [] }),
    });
    expect(rest).toHaveBeenCalledWith("/dm/alpha/open", { method: "POST", body: { thread_id: "t1", messages: [] } });
  });

  it("rebuilds the 503 setup_needed answer, from statusCode and from the message across IPC", async () => {
    const body = JSON.stringify({ detail: "Emporia is not set up in this profile", setup_needed: true });
    for (const viaIpc of [false, true]) {
      const r = await restFetch(bridge(async () => { throw httpError(503, body, viaIpc); }), "/api/plugins/emporia/agents");
      expect(r.status).toBe(503);
      expect(r.ok).toBe(false);
      expect(await r.json()).toEqual({ detail: "Emporia is not set up in this profile", setup_needed: true });
    }
  });

  it("reads the Webapp's error the same way (`<status>: <body>`, no statusCode)", async () => {
    const r = await restFetch(bridge(async () => { throw new Error('405: {"detail":"the dashboard only reads"}'); }),
      "/api/plugins/emporia/relay/alpha/bounties");
    expect(r.status).toBe(405);
    expect((await r.json()).detail).toBe("the dashboard only reads");
  });

  it("rejects a failure with no HTTP status, as fetch rejects a network error", async () => {
    await expect(restFetch(bridge(async () => { throw new Error("Timed out connecting to profile"); }),
      "/api/plugins/emporia/agents")).rejects.toThrow("Timed out");
    expect(statusOf(new Error("Hermes API returned HTML for /x"))).toBeNull();
  });

  it("never sends a request outside the plugin's namespace", async () => {
    const rest = vi.fn();
    const r = await restFetch(bridge(rest as unknown as DesktopBridge["rest"]), "/api/profiles");
    expect(rest).not.toHaveBeenCalled();
    expect(r.status).toBe(404);
  });
});

describe("PluginSocket: a receive-only WebSocket over ctx.socket", () => {
  it("opens on the first frame, hands frames on as text, drops sends, and ends on close", () => {
    let deliver: (d: unknown) => void = () => {};
    const dispose = vi.fn();
    const socket = vi.fn((_path: string, onMessage: (d: unknown) => void) => { deliver = onMessage; return dispose; });
    const ws = new PluginSocket(bridge(async () => null as never, socket), "/ws/alpha/ws/events");
    expect(socket).toHaveBeenCalledWith("/ws/alpha/ws/events", expect.any(Function));
    expect(ws.readyState).toBe(PluginSocket.CONNECTING);
    const opened = vi.fn();
    const got: unknown[] = [];
    ws.onopen = opened;
    ws.onmessage = (e) => got.push(JSON.parse(e.data));
    deliver({ type: "connected", health: { status: "ok" } });
    deliver({ type: "session_created", session_id: "s1" });
    expect(opened).toHaveBeenCalledTimes(1);
    expect(ws.readyState).toBe(PluginSocket.OPEN);
    expect(got).toEqual([{ type: "connected", health: { status: "ok" } }, { type: "session_created", session_id: "s1" }]);
    expect(() => ws.send('{"type":"ping"}')).not.toThrow();
    ws.close();
    expect(dispose).toHaveBeenCalledTimes(1);
    deliver({ type: "late" });
    expect(got).toHaveLength(2);
    ws.close();
    expect(dispose).toHaveBeenCalledTimes(1);
  });
});
