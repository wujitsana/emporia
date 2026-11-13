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

/** Chain id -> label and address-explorer prefix (as `networks.py` has them). */
export const CHAINS: Record<string, { label: string; explorer: string }> = {
  "84532": { label: "Base Sepolia", explorer: "https://sepolia.basescan.org/address/" },
  "8453": { label: "Base", explorer: "https://basescan.org/address/" },
  "46630": { label: "Robinhood Chain Testnet", explorer: "https://explorer.testnet.chain.robinhood.com/address/" },
  "4663": { label: "Robinhood Chain", explorer: "https://robinhoodchain.blockscout.com/address/" },
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
  cached: boolean;
  registration: { name?: string; description?: string; flags?: string[]; active?: boolean | null; note?: string } | null;
  image: { source: string; available: boolean; note: string; type: string };
  image_url: string;
}

/** "checked by you at block N", from this machine's own read; never the relay's word. */
export function localStatus(v: ChainView): string {
  if (v.status === "checked") return `checked by you at block ${v.block_number}`;
  if (v.status === "not-proven") return `your read does not show it${v.reason ? `: ${v.reason}` : ""}`;
  if (v.status === "not-checked") return "not checked here";
  return "your read could not reach the chain just now";
}

function query(l: ChainLink): string {
  return `registry=${encodeURIComponent(l.agent_registry)}&agent=${encodeURIComponent(l.agent_id)}&signer=${encodeURIComponent(l.signer)}`;
}

/** The viewer's own read of a link, through the local console; null where there is no console (or it did not answer). */
export async function fetchChainView(viewer: string | null, l: ChainLink): Promise<ChainView | null> {
  if (!VIA_CONSOLE || !viewer) return null;
  try {
    const r = await httpFetch(consoleUrl(`/chain-link/${encodeURIComponent(viewer)}?${query(l)}`));
    return r.ok ? ((await r.json()) as ChainView) : null;
  } catch {
    return null;
  }
}

/** The image's address on the console (same origin). Used as an `<img src>` where the page carries a cookie; inside a
 *  host it is fetched as bytes (`fetchChainImage`) because an `<img>` cannot send the host's sign-in. */
export function chainImagePath(viewer: string, l: ChainLink): string {
  return consoleUrl(`/chain-image/${encodeURIComponent(viewer)}?${query(l)}`);
}

export async function fetchChainImage(viewer: string, l: ChainLink): Promise<Blob | null> {
  try {
    const r = await httpFetch(chainImagePath(viewer, l));
    if (!r.ok) return null;
    const b = await r.blob();
    return b.type.startsWith("image/") ? b : null;
  } catch {
    return null;
  }
}
