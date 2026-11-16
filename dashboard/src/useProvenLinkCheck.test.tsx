// @vitest-environment jsdom
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentProfile, ChainLink } from "./api";

const fetchView = vi.fn();
vi.mock("./relayEnv", () => ({ RELAY: "http://relay.test", VIEWER_ID: "me" }));
vi.mock("./transport", () => ({ IN_HERMES: false, VIA_CONSOLE: true, consoleUrl: (p: string) => p, httpFetch: vi.fn() }));
vi.mock("./chainlink", async (orig) => ({ ...(await orig<typeof import("./chainlink")>()), fetchChainView: (...a: unknown[]) => fetchView(...a) }));
const refresh = vi.fn();
vi.mock("./agentPicture", () => ({ refreshChainPictures: () => refresh() }));

import { useProvenLinkCheck } from "./useProvenLinkCheck";

const link = (over: Partial<ChainLink> = {}): ChainLink => ({
  site: "erc8004", agent_registry: "eip155:84532:0x8004a818bfb912233c491871b3d84c89a494bd9e", agent_id: "42", signer: "0x" + "11".repeat(20),
  tier: "owner", status: "checked", checked: { block_number: 1, block_hash: "0xab", at: "x" }, confirmed_by: 0, testnet: true, ...over });
const agent = (links: AgentProfile["links"]) => ({ agent_id: "beta", display_name: "beta", registered_at: "x", is_active: true,
  session_count: 0, win_count: 0, stable_id: "ab".repeat(32), links }) as AgentProfile;
function Probe({ a }: { a: AgentProfile }) { useProvenLinkCheck(a); return null; }

afterEach(() => { cleanup(); fetchView.mockReset(); refresh.mockReset(); });

describe("the profile's check of its links at load", () => {
  it("asks the console to check each live link, then tells the pictures to look again", async () => {
    fetchView.mockResolvedValue(null);
    render(<Probe a={agent([link(), link({ agent_id: "7", status: "superseded" })])} />);
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
    expect(fetchView).toHaveBeenCalledTimes(1);
    expect(fetchView.mock.calls[0][0]).toBe("me");
    expect(fetchView.mock.calls[0][2]).toBe("ab".repeat(32));
  });
  it("does nothing for an agent with no link", async () => {
    render(<Probe a={agent([])} />);
    await new Promise((r) => setTimeout(r, 10));
    expect(fetchView).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });
});
