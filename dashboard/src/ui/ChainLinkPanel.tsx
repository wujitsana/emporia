import { useEffect, useState } from "react";
import Text from "@kit/Text";
import type { AgentProfile, ChainLink } from "../api";
import { VIEWER_ID } from "../relayEnv";
import { IN_HERMES, VIA_CONSOLE } from "../transport";
import {
  badgeLine, chainImagePath, chainLinks, explorerUrl, fetchChainImage, fetchChainView, localStatus, relayStatus,
  type ChainView,
} from "../chainlink";
import { MetaGrid } from "./cards";
import { CopyCommand } from "./CopyCommand";

/** One line, `8004 #N · chain · what is proven`, with the explorer link; the badge a list or a hero shows. */
export function ChainBadge({ link }: { link: ChainLink }) {
  const url = explorerUrl(link);
  return (
    <span className="e-quiet-chip" data-testid="chain-badge" data-link-status={link.status}
      title="The agent says it controls this ERC-8004 identity; the badge states only what was proven, and carries no weight here">
      {badgeLine(link)}
      {link.status === "stale" ? " · stale" : ""}
      {url ? <>{" "}<a href={url} target="_blank" rel="noopener noreferrer" aria-label="registry on the chain's explorer">↗</a></> : null}
    </span>
  );
}

/** The badges of an agent's live links (a superseded one has none). Lists and the hero show these and nothing more. */
export function ChainBadges({ agent }: { agent: Pick<AgentProfile, "links"> }) {
  return <>{chainLinks(agent).filter((l) => l.status !== "superseded").map((l) => <ChainBadge key={`${l.agent_registry}/${l.agent_id}`} link={l} />)}</>;
}

/** The picture, from an on-chain data: URI the local console read. Shown in an <img>, never inline, never fetched
 *  from a URL by this page. Inside Hermes the bytes come through its authed fetch and are shown from a blob. */
function ChainImage({ link, stable }: { link: ChainLink; stable?: string }) {
  const [src, setSrc] = useState<string | null>(VIEWER_ID && !IN_HERMES ? chainImagePath(VIEWER_ID, link, stable) : null);
  useEffect(() => {
    if (!VIEWER_ID || !IN_HERMES) return;
    let url: string | null = null;
    let live = true;
    void fetchChainImage(VIEWER_ID, link, stable).then((b) => {
      if (live && b) { url = URL.createObjectURL(b); setSrc(url); }
    });
    return () => { live = false; if (url) URL.revokeObjectURL(url); };
  }, [link, stable]);
  if (!src) return null;
  return <img className="e-chain-image" src={src} width={96} height={96} alt="" referrerPolicy="no-referrer" loading="lazy" decoding="async"
    data-testid="chain-image" style={{ imageRendering: "auto", objectFit: "contain", background: "var(--theme-background-input, transparent)" }} />;
}

function Registration({ view }: { view: ChainView }) {
  const r = view.registration;
  if (!r || (!r.name && !r.description)) return null;
  return (
    <Text className="e-dim" style={{ display: "block", fontSize: "0.72rem" }} data-testid="chain-registration"
      title="The registration's own words, written by whoever owns the identity: never this agent's name, never an instruction">
      {r.name ? <>“{r.name}”</> : null}{r.name && r.description ? " — " : null}{r.description}
      {" "}<span className="e-faint">(text on its registration, not verified{r.active === false ? "; inactive" : ""})</span>
      {r.flags?.length ? <span className="e-faint" title="This text reads like an instruction to an agent. Treat it as text."> · ⚠ reads like an instruction</span> : null}
    </Text>
  );
}

function ChainLinkRow({ link, stable }: { link: ChainLink; stable?: string }) {
  const [view, setView] = useState<ChainView | null>(null);
  useEffect(() => {
    let live = true;
    if (VIA_CONSOLE && VIEWER_ID && link.status !== "superseded") {
      void fetchChainView(VIEWER_ID, link, stable).then((v) => { if (live) setView(v); });
    }
    return () => { live = false; };
  }, [link, stable]);
  return (
    <div className="e-chain-link" data-testid="chain-link-row" style={{ display: "grid", gap: 4 }}>
      <ChainBadge link={link} />
      <Text className="e-faint" style={{ fontSize: "0.68rem" }}>
        <span data-testid="chain-relay-status">{relayStatus(link)}</span>
        {view ? <> · <span data-testid="chain-local-status">{localStatus(view)}</span></> : null}
      </Text>
      {view ? <Registration view={view} /> : null}
      {view?.image.available ? <ChainImage link={link} stable={stable} /> : null}
      {view && !view.image.available && view.status === "checked" && view.image.note
        ? <Text className="e-faint" style={{ fontSize: "0.66rem" }}>{view.image.note}</Text> : null}
    </div>
  );
}

/** The profile's ERC-8004 section: each link, the relay's status beside this machine's own read, the registration
 *  text (secondary) and the on-chain image. For the viewer's own agent with none, the command that starts one. */
export function ChainLinkPanel({ agent }: { agent: AgentProfile }) {
  const links = chainLinks(agent);
  const own = VIEWER_ID !== null && VIEWER_ID === agent.agent_id;
  if (!links.length && !own) return null;
  return (
    <div className="e-xlink" data-testid="chain-link-panel">
      {links.length ? (
        <MetaGrid rows={links.map((l) => ["ERC-8004", <ChainLinkRow key={`${l.agent_registry}/${l.agent_id}`} link={l} stable={agent.stable_id} />])} />
      ) : (
        <div style={{ marginTop: 10, marginBottom: 14 }}>
          <Text className="e-dim" style={{ fontSize: "0.68rem" }}>
            No ERC-8004 identity linked. To link one you already have (nothing goes on-chain, no money moves), run in a terminal:
          </Text>
          <div className="e-op-cmds">
            <CopyCommand command={`emporia agent link-8004 ${agent.agent_id} --chain base --agent-id N`} />
          </div>
        </div>
      )}
    </div>
  );
}
