/**
 * An agent's ERC-8004 link on its card: how the dashboard words it, and how it asks the local console for its own
 * read, the registration text and the image.
 *
 * The relay stores and serves a signed link and the status of its own read: no image, no registration text. This client
 * builds the explorer link (a table below, kept equal to `networks.py`'s address explorers by a test) and, where a local
 * console is in front, asks it for "checked by you at block N" and the on-chain image. The dashboard signs and writes
 * nothing: the console's two routes only read.
 */
import type { AgentProfile, CardLink, ChainLink } from "./api";
import { consoleUrl, httpFetch, VIA_CONSOLE } from "./transport";

/** Chain id -> label, address-explorer prefix and token-page pattern (as `networks.py` has them). */
export const CHAINS: Record<string, { label: string; explorer: string; token: string }> = {
  "84532": { label: "Base Sepolia", explorer: "https://sepolia.basescan.org/address/", token: "https://sepolia.basescan.org/nft/{addr}/{id}" },
  "8453": { label: "Base", explorer: "https://basescan.org/address/", token: "https://basescan.org/nft/{addr}/{id}" },
  "46630": { label: "Robinhood Chain Testnet", explorer: "https://explorer.testnet.chain.robinhood.com/address/", token: "https://explorer.testnet.chain.robinhood.com/token/{addr}/instance/{id}" },
  "4663": { label: "Robinhood Chain", explorer: "https://robinhoodchain.blockscout.com/address/", token: "https://robinhoodchain.blockscout.com/token/{addr}/instance/{id}" },
};

const TIER_WORDS: Record<string, string> = {
  owner: "owner",
  "bound-owner": "holder of the bound token",
  controller: "controller, per the owning contract",
};

export function chainLinks(a: Pick<AgentProfile, "links">): ChainLink[] {
  return (a.links ?? []).filter((l: CardLink): l is ChainLink => l.site === "erc8004");
}

const parts = (l: Pick<ChainLink, "agent_registry">) => l.agent_registry.split(":");

export function chainLabel(l: Pick<ChainLink, "agent_registry">): string {
  const id = parts(l)[1] ?? "";
  return CHAINS[id]?.label ?? `chain ${id}`;
}

/** What is proven, in words; a link that was only claimed says so. */
export function tierWording(l: Pick<ChainLink, "tier" | "status">): string {
  return TIER_WORDS[l.tier] ?? (l.status === "superseded" ? "superseded" : "claimed");
}

/** The primary line: `8004 #N · <chain label> · <tier wording>`, with "(testnet)" for a test chain. */
export function badgeLine(l: ChainLink): string {
  return `8004 #${l.agent_id} · ${chainLabel(l)} · ${tierWording(l)}${l.testnet ? " (testnet)" : ""}`;
}

/** The registry's page on the chain's explorer, built here from the chain id and the registry address; "" when
 *  the chain or the address is not one this client knows (a hostile card never picks a URL). */
export function explorerUrl(l: Pick<ChainLink, "agent_registry">): string {
  const [, id, addr] = parts(l);
  const base = CHAINS[id ?? ""]?.explorer;
  return base && /^0x[0-9a-f]{40}$/.test(addr ?? "") ? `${base}${addr}` : "";
}

/** The 8004 identity's own page: it is an ERC-721 on the registry, so the explorer's token page (registry address and
 *  agent number), not the registry's address page. Built here; "" for an unknown chain, address or number. */
export function tokenUrl(l: Pick<ChainLink, "agent_registry" | "agent_id">): string {
  const [, id, addr] = parts(l);
  const pattern = CHAINS[id ?? ""]?.token;
  return pattern && /^0x[0-9a-f]{40}$/.test(addr ?? "") && /^[0-9]{1,78}$/.test(l.agent_id)
    ? pattern.replace("{addr}", addr!).replace("{id}", l.agent_id) : "";
}

/** The relay's own status, for a reader who should not take it as more than it is. */
export function relayStatus(l: ChainLink): string {
  const at = l.checked ? ` at block ${l.checked.block_number}` : "";
  switch (l.status) {
    case "checked": return `checked by this relay${at}`;
    case "stale": return `no longer proven: the last good read was${at || " never"}`;
    case "superseded": return "taken over by another agent's newer link";
    default: return "claimed: this relay has not read the chain for it";
  }
}

/** What the console's `GET /chain-link/<agent>` answers. */
export interface ChainView {
  status: "checked" | "not-proven" | "unknown" | "not-checked" | string;
  reason: string;
  tier: string;
  tier_wording: string;
  owner: string;
  block_number: number | null;
  block_hash: string;
  checked_at: number;
  /** True when the console verified the wallet's signature too, not only the chain's control. */
  signature_checked?: boolean;
  /** True when the registration, read on-chain at the same block, names this agent's stable id (never fetched from a URL). */
  two_way?: boolean;
  /** The registry's getAgentWallet at that block ('' when unset). */
  agent_wallet?: string;
  /** True when that is the agent's own registered EVM wallet (visibility only; never proof of control). */
  paid_at_own_wallet?: boolean | null;
  cached: boolean;
  registration: { name?: string; description?: string; flags?: string[]; active?: boolean | null; note?: string } | null;
  image: { source: string; available: boolean; note: string; type: string };
  image_url: string;
}

/** "checked by you at block N", from this machine's own read; never the relay's word. */
export function localStatus(v: ChainView, bare = false): string {
  if (v.status === "checked") {
    // `bare`: the identity card says two-way and where it is paid on its own lines
    const two = bare ? "" : (v.two_way ? " · two-way" : "") + (v.paid_at_own_wallet ? " · paid at its own wallet" : "");
    return v.signature_checked === false ? `chain control checked by you at block ${v.block_number} (signature not available)${two}`
      : `checked by you at block ${v.block_number}${two}`;
  }
  if (v.status === "not-proven") return `your read does not show it${v.reason ? `: ${v.reason}` : ""}`;
  if (v.status === "not-checked") return "not checked here";
  return "your read could not reach the chain just now";
}

/** The query for the console's routes. With the card's stable id and the link's `issued_at` and `wallet_sig` the
 *  console verifies the wallet's signature as well as the chain's control. */
function query(l: ChainLink, stable?: string, payTo?: string): string {
  const pay = payTo && /^0x[0-9a-fA-F]{40}$/.test(payTo) ? `&pay_to=${payTo}` : "";
  const base = `registry=${encodeURIComponent(l.agent_registry)}&agent=${encodeURIComponent(l.agent_id)}&signer=${encodeURIComponent(l.signer)}${pay}`;
  if (!stable || !l.wallet_sig || !l.issued_at || !l.agent_name) return base;
  return `${base}&stable=${encodeURIComponent(stable)}&issued=${l.issued_at}&name=${encodeURIComponent(l.agent_name)}&sig=${encodeURIComponent(l.wallet_sig)}`;
}

/** The viewer's own read of a link, through the local console; null where there is no console (or it did not answer). */
export async function fetchChainView(viewer: string | null, l: ChainLink, stable?: string, payTo?: string): Promise<ChainView | null> {
  if (!VIA_CONSOLE || !viewer) return null;
  try {
    const r = await httpFetch(consoleUrl(`/chain-link/${encodeURIComponent(viewer)}?${query(l, stable, payTo)}`));
    return r.ok ? ((await r.json()) as ChainView) : null;
  } catch {
    return null;
  }
}

/** The image's address on the console (same origin). Used as an `<img src>` where the page carries a cookie; inside a
 *  host it is fetched as bytes (`fetchChainImage`) because an `<img>` cannot send the host's sign-in. */
export function chainImagePath(viewer: string, l: ChainLink, stable?: string): string {
  return consoleUrl(`/chain-image/${encodeURIComponent(viewer)}?${query(l, stable)}`);
}

export async function fetchChainImage(viewer: string, l: ChainLink, stable?: string): Promise<Blob | null> {
  try {
    const r = await httpFetch(chainImagePath(viewer, l, stable));
    if (!r.ok) return null;
    const b = await r.blob();
    return b.type.startsWith("image/") ? b : null;
  } catch {
    return null;
  }
}
