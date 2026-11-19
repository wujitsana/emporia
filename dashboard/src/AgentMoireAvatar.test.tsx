// @vitest-environment jsdom
import { cleanup, render, waitFor } from "@testing-library/react";
import { sha256 } from "@noble/hashes/sha2.js";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const transport = vi.hoisted(() => ({ via: true, hermes: true, httpFetch: vi.fn() }));
vi.mock("./relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "me" }));
vi.mock("./transport", () => ({
  get IN_HERMES() { return transport.hermes; }, get VIA_CONSOLE() { return transport.via; }, consoleUrl: (p: string) => p, httpFetch: transport.httpFetch,
}));

import { AgentMoireAvatar } from "./AgentMoireAvatar";
import { refreshChainPictures, resetPicturesForTests } from "./agentPicture";
import { tokenAvatar } from "./avatars";
import { bytesToHex } from "./encoding";

const sha = (s: string) => bytesToHex(sha256(new TextEncoder().encode(s)));
const STABLE = sha("agent-one");
const inner = (svg: string) => svg.slice(svg.indexOf(">") + 1, svg.lastIndexOf("</svg>"));
const png = () => new Response(new Uint8Array([0x89, 0x50, 0x4e, 0x47]), { status: 200, headers: { "content-type": "image/png" } });
/** What the browser makes of a generated SVG string, so two renderings compare the same way. */
const parsed = (svg: string) => { const d = document.createElementNS("http://www.w3.org/2000/svg", "svg"); d.innerHTML = inner(svg); return d.innerHTML; };

beforeEach(() => {
  transport.via = true;
  transport.hermes = true;
  URL.createObjectURL = vi.fn(() => "blob:chain-picture");
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => { cleanup(); resetPicturesForTests(); transport.httpFetch.mockReset(); });

describe("the avatar", () => {
  it("draws the token design from a stable id, at the size asked", () => {
    const { container } = render(<AgentMoireAvatar agentId="beta" stableId={STABLE} size={40} link={false} />);
    const svg = container.querySelector("svg")!;
    expect(svg.getAttribute("width")).toBe("40");
    expect(svg.getAttribute("viewBox")).toBe("0 0 32 32");
    expect(svg.innerHTML).toBe(parsed(tokenAvatar(STABLE)));
  });

  it("draws from the SHA-256 of the handle where only the handle is known", () => {
    const { container } = render(<AgentMoireAvatar agentId="beta" link={false} />);
    expect(container.querySelector("svg")!.innerHTML).toBe(parsed(tokenAvatar(sha("beta"))));
    expect(transport.httpFetch).not.toHaveBeenCalled();
  });

  it("is deterministic and carries no ids, so many on one page never collide", () => {
    const { container } = render(<>{[0, 1, 2, 3].map((i) => <AgentMoireAvatar key={i} agentId="beta" stableId={STABLE} size={18 + i} link={false} />)}</>);
    expect(container.querySelectorAll("[id]")).toHaveLength(0);
    const insides = [...container.querySelectorAll("svg")].map((v) => v.innerHTML);
    expect(new Set(insides).size).toBe(1);
  });

  it("shows the on-chain image only for an agent whose proven link the console already holds", async () => {
    transport.httpFetch.mockResolvedValue(png());
    const { container, findByTestId } = render(<AgentMoireAvatar agentId="beta" stableId={STABLE} link={false} />);
    expect(container.querySelector("svg")).not.toBeNull();   // the everyday picture first
    const img = await findByTestId("avatar-onchain");
    expect(img.getAttribute("src")).toBe("blob:chain-picture");
    expect(img.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(transport.httpFetch.mock.calls[0][0]).toBe(`/chain-picture/me?stable=${STABLE}`);   // the cache-only route
  });

  it("outside a host the image's address is the console's own route (the page's policy allows no blob: images)", async () => {
    transport.hermes = false;
    transport.httpFetch.mockResolvedValue(png());
    const { findByTestId } = render(<AgentMoireAvatar agentId="beta" stableId={STABLE} link={false} />);
    expect((await findByTestId("avatar-onchain")).getAttribute("src")).toBe(`/chain-picture/me?stable=${STABLE}`);
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("keeps the everyday picture when the console holds no proven link (204), or an answer that is not an image", async () => {
    transport.httpFetch.mockResolvedValue(new Response(null, { status: 204 }));
    const { container } = render(<AgentMoireAvatar agentId="beta" stableId={STABLE} link={false} />);
    await waitFor(() => expect(transport.httpFetch).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 10));
    expect(container.querySelector("svg")).not.toBeNull();
    expect(container.querySelector("img")).toBeNull();
    cleanup(); resetPicturesForTests();
    transport.httpFetch.mockResolvedValue(new Response("<html>", { status: 200, headers: { "content-type": "text/html" } }));
    const again = render(<AgentMoireAvatar agentId="beta" stableId={STABLE} link={false} />);
    await new Promise((r) => setTimeout(r, 10));
    expect(again.container.querySelector("img")).toBeNull();
  });

  it("asks once for many avatars of one agent, and never without a stable id or a console", async () => {
    transport.httpFetch.mockResolvedValue(png());
    render(<>{[0, 1, 2].map((i) => <AgentMoireAvatar key={i} agentId="beta" stableId={STABLE} link={false} />)}</>);
    await waitFor(() => expect(transport.httpFetch).toHaveBeenCalledTimes(1));
    cleanup(); resetPicturesForTests(); transport.httpFetch.mockClear();
    transport.via = false;
    render(<AgentMoireAvatar agentId="beta" stableId={STABLE} link={false} />);
    render(<AgentMoireAvatar agentId="gamma" link={false} />);
    await new Promise((r) => setTimeout(r, 10));
    expect(transport.httpFetch).not.toHaveBeenCalled();
  });

  it("goes back to the everyday picture when the link is checked again and no longer proven", async () => {
    transport.httpFetch.mockResolvedValueOnce(png());
    const { container, findByTestId } = render(<AgentMoireAvatar agentId="beta" stableId={STABLE} link={false} />);
    await findByTestId("avatar-onchain");
    transport.httpFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));
    refreshChainPictures();
    await waitFor(() => expect(container.querySelector("img")).toBeNull());
    expect(container.querySelector("svg")).not.toBeNull();
  });

  it("reuses a stable id learned elsewhere on the page for a handle-only avatar", () => {
    const { container } = render(<><AgentMoireAvatar agentId="beta" stableId={STABLE} link={false} /><AgentMoireAvatar agentId="beta" link={false} /></>);
    const [a, b] = [...container.querySelectorAll("svg")];
    const strip = (s: string) => s.replace(/id="([ab])[^"]+"/g, 'id="$1"').replace(/#([ab])[A-Za-z0-9]+\)/g, "#$1)");
    expect(strip(a.innerHTML)).toBe(strip(b.innerHTML));
  });
});
