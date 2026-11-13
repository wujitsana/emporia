import { describe, expect, it } from "vitest";
import type { ChainLink } from "./api";
import { badgeLine, chainImagePath, CHAINS, chainLinks, explorerUrl, localStatus, relayStatus, tierWording, type ChainView } from "./chainlink";
import { xLinks } from "./xlink";

const REG = "0x8004a818bfb912233c491871b3d84c89a494bd9e";
const link = (over: Partial<ChainLink> = {}): ChainLink => ({
  site: "erc8004", agent_registry: `eip155:84532:${REG}`, agent_id: "42", signer: "0x" + "11".repeat(20), tier: "owner",
  status: "checked", checked: { block_number: 123, block_hash: "0xab", at: "2026-11-13T00:00:00Z" }, confirmed_by: 0,
  testnet: true, ...over,
});

describe("an ERC-8004 link's wording", () => {
  it("is `8004 #N · chain · what is proven`, with (testnet) on a test chain only", () => {
    expect(badgeLine(link())).toBe("8004 #42 · Base Sepolia · owner (testnet)");
    expect(badgeLine(link({ agent_registry: `eip155:8453:${REG}`, testnet: false, tier: "bound-owner" })))
      .toBe("8004 #42 · Base · holder of the bound token");
    expect(badgeLine(link({ agent_registry: `eip155:46630:${REG}`, tier: "controller" })))
      .toBe("8004 #42 · Robinhood Chain Testnet · controller, per the owning contract (testnet)");
    expect(badgeLine(link({ tier: "", status: "claimed" }))).toBe("8004 #42 · Base Sepolia · claimed (testnet)");
  });

  it("keeps a uint256 agent id as the text it is", () => {
    const big = "115792089237316195423570985008687907853269984665640564039457584007913129639935";
    expect(badgeLine(link({ agent_id: big }))).toContain(`#${big} `);
    expect(tierWording({ tier: "", status: "superseded" })).toBe("superseded");
  });

  it("builds the explorer link from the chain and the registry, and none for anything it does not know", () => {
    expect(explorerUrl(link())).toBe(`https://sepolia.basescan.org/address/${REG}`);
    expect(explorerUrl(link({ agent_registry: `eip155:4663:${REG}` }))).toBe(`https://robinhoodchain.blockscout.com/address/${REG}`);
    expect(explorerUrl(link({ agent_registry: `eip155:1:${REG}` }))).toBe("");
    expect(explorerUrl(link({ agent_registry: "eip155:84532:javascript:alert(1)" }))).toBe("");
  });

  it("says what the relay read and never more than that", () => {
    expect(relayStatus(link())).toBe("checked by this relay at block 123");
    expect(relayStatus(link({ status: "claimed", tier: "", checked: null }))).toBe("claimed: this relay has not read the chain for it");
    expect(relayStatus(link({ status: "stale" }))).toContain("no longer proven");
    expect(relayStatus(link({ status: "superseded" }))).toContain("newer link");
  });

  it("says 'checked by you at block N' only from this machine's own read", () => {
    const v = { status: "checked", block_number: 321, reason: "" } as ChainView;
    expect(localStatus(v)).toBe("checked by you at block 321");
    expect(localStatus({ ...v, status: "not-proven", reason: "the signer does not own the agent" } as ChainView)).toContain("does not show it");
    expect(localStatus({ ...v, status: "unknown" } as ChainView)).toContain("could not reach");
  });

  it("says when the wallet's signature was not part of the check", () => {
    const v = { status: "checked", block_number: 321, reason: "" } as ChainView;
    expect(localStatus({ ...v, signature_checked: true })).toBe("checked by you at block 321");
    expect(localStatus({ ...v, signature_checked: false })).toContain("signature not available");
  });

  it("sends the card's signature to the console only with the stable id, and as encoded query values", () => {
    const l = link({ issued_at: 1700000000, wallet_sig: "0xab" });
    expect(chainImagePath("me", l)).not.toContain("sig=");
    expect(chainImagePath("me", { ...l, wallet_sig: undefined }, "0x" + "11".repeat(32))).not.toContain("sig=");
    const url = chainImagePath("me", l, "0x" + "11".repeat(32));
    expect(url).toContain("issued=1700000000");
    expect(url).toContain("sig=0xab");
    expect(url).toContain(`stable=0x${"11".repeat(32)}`);
  });

  it("separates X accounts from chain links on one card", () => {
    const card = { links: [link(), { site: "x", handle: "kestrel_x", post_id: "bio", status: "claimed", confirmed_by: 0 }] as never };
    expect(chainLinks(card)).toHaveLength(1);
    expect(xLinks(card).map((l) => l.handle)).toEqual(["kestrel_x"]);
  });

  it("knows the same chains as the runtime's table", () => {
    expect(Object.keys(CHAINS).sort()).toEqual(["46630", "4663", "8453", "84532"].sort());
  });
});
