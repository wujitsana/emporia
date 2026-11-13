// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentProfile, ChainLink } from "../api";

const fetchView = vi.fn();
vi.mock("../relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "me" }));
vi.mock("../transport", () => ({ IN_HERMES: false, VIA_CONSOLE: true, consoleUrl: (p: string) => p, httpFetch: vi.fn() }));
vi.mock("../chainlink", async (orig) => ({ ...(await orig<typeof import("../chainlink")>()), fetchChainView: (...a: unknown[]) => fetchView(...a) }));

import { ChainBadges, ChainLinkPanel } from "./ChainLinkPanel";

const REG = "0x8004a818bfb912233c491871b3d84c89a494bd9e";
const link = (over: Partial<ChainLink> = {}): ChainLink => ({
  site: "erc8004", agent_registry: `eip155:84532:${REG}`, agent_id: "42", signer: "0x" + "11".repeat(20), tier: "owner",
  status: "checked", checked: { block_number: 123, block_hash: "0xab", at: "x" }, confirmed_by: 0, testnet: true, ...over,
});
const agent = (links: AgentProfile["links"]) => ({ agent_id: "beta", display_name: "beta", registered_at: "x", is_active: true,
  session_count: 0, win_count: 0, links }) as AgentProfile;
const view = (over: Record<string, unknown> = {}) => ({ status: "checked", reason: "", tier: "owner", tier_wording: "owner", owner: "0x1",
  block_number: 321, block_hash: "0xcd", checked_at: 1, cached: false, registration: null,
  image: { source: "none", available: false, note: "image hosted off-chain", type: "" }, image_url: "", ...over });

afterEach(() => { cleanup(); fetchView.mockReset(); });

describe("the ERC-8004 badge and panel", () => {
  it("renders the badge line with an explorer link, and lists show nothing more", () => {
    render(<ChainBadges agent={agent([link()])} />);
    const badge = screen.getByTestId("chain-badge");
    expect(badge.textContent).toContain("8004 #42 · Base Sepolia · owner (testnet)");
    expect(badge.querySelector("a")?.getAttribute("href")).toBe(`https://sepolia.basescan.org/address/${REG}`);
    expect(badge.querySelector("a")?.getAttribute("rel")).toContain("noopener");
    expect(screen.queryByTestId("chain-image")).toBeNull();
  });

  it("shows a mainnet link without (testnet), and no badge for a superseded one", () => {
    render(<ChainBadges agent={agent([link({ agent_registry: `eip155:8453:${REG}`, testnet: false }), link({ agent_id: "7", status: "superseded", tier: "" })])} />);
    const badges = screen.getAllByTestId("chain-badge");
    expect(badges).toHaveLength(1);
    expect(badges[0].textContent).toContain("8004 #42 · Base · owner");
    expect(badges[0].textContent).not.toContain("testnet");
  });

  it("shows the relay's status beside the viewer's own read, and the registration text as a stranger's", async () => {
    fetchView.mockResolvedValue(view({ registration: { name: "Chad", description: "hello", flags: ["injection-pattern"], active: false } }));
    render(<ChainLinkPanel agent={agent([link()])} />);
    expect(screen.getByTestId("chain-relay-status").textContent).toBe("checked by this relay at block 123");
    await waitFor(() => expect(screen.getByTestId("chain-local-status").textContent).toBe("checked by you at block 321"));
    const reg = screen.getByTestId("chain-registration");
    expect(reg.textContent).toContain("“Chad”");
    expect(reg.textContent).toContain("not verified");
    expect(reg.textContent).toContain("reads like an instruction");
    expect(reg.textContent).toContain("inactive");
  });

  it("loads the image on the profile only when the console says an on-chain one exists", async () => {
    fetchView.mockResolvedValue(view({ image: { source: "registration", available: true, note: "", type: "image/png" },
      image_url: "/chain-image/me" }));
    render(<ChainLinkPanel agent={agent([link()])} />);
    const img = await screen.findByTestId("chain-image");
    expect(img.tagName).toBe("IMG");
    expect(img.getAttribute("src")).toContain("/chain-image/me?registry=");
    expect(img.getAttribute("referrerpolicy")).toBe("no-referrer");
  });

  it("shows no image and the reason when it is hosted off-chain", async () => {
    fetchView.mockResolvedValue(view());
    render(<ChainLinkPanel agent={agent([link()])} />);
    await screen.findByText("image hosted off-chain");
    expect(screen.queryByTestId("chain-image")).toBeNull();
  });

  it("offers the command to link one to the viewer's own agent, and nothing to anyone else's", () => {
    render(<ChainLinkPanel agent={{ ...agent([]), agent_id: "me" }} />);
    expect(screen.getByText(/emporia agent link-8004 me --chain base/)).toBeTruthy();
    cleanup();
    render(<ChainLinkPanel agent={agent([])} />);
    expect(screen.queryByTestId("chain-link-panel")).toBeNull();
  });
});
