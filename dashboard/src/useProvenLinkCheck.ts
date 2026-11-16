import { useEffect } from "react";
import type { AgentProfile } from "./api";
import { chainLinks, fetchChainView } from "./chainlink";
import { refreshChainPictures } from "./agentPicture";
import { VIEWER_ID } from "./relayEnv";
import { VIA_CONSOLE } from "./transport";

/** On a profile page: the console's own check of each live link, at page load, so the hero's picture can be the
 *  proven token's on-chain image without opening the About tab. It is the same read the panel makes (the console
 *  caches it ten minutes, so the panel's later look costs nothing more); lists never make it. */
export function useProvenLinkCheck(agent: AgentProfile): void {
  const links = chainLinks(agent).filter((l) => l.status !== "superseded");
  const key = links.map((l) => `${l.agent_registry}/${l.agent_id}/${l.signer}/${l.wallet_sig ?? ""}`).join("|");
  const { stable_id: stable, x402_pay_to } = agent;
  const payTo = x402_pay_to?.evm;
  useEffect(() => {
    if (!VIA_CONSOLE || !VIEWER_ID || !key) return;
    void Promise.all(links.map((l) => fetchChainView(VIEWER_ID!, l, stable, payTo))).then(() => refreshChainPictures());
    // `links` is a function of `key`
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [key, stable, payTo]);
}
