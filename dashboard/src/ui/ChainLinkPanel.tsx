import { useEffect, useState, type ReactNode } from "react";
import Text from "@kit/Text";
import type { AgentProfile, ChainLink } from "../api";
import { VIEWER_ID } from "../relayEnv";
import { IN_HERMES, VIA_CONSOLE } from "../transport";
import {
  badgeLine, chainImagePath, chainLabel, chainLinks, explorerUrl, fetchChainImage, fetchChainView, localStatus, relayStatus, tierWording, tokenUrl,
  type ChainView,
} from "../chainlink";
import { refreshChainPictures } from "../agentPicture";
import { Card, SectionHead } from "./HomeCards";
import { CopyCommand } from "./CopyCommand";

/** One line, `8004 #N · chain · what is proven`; the badge a list or a hero shows. The explorer link is on the
 *  identity card only (About), never here. */
export function ChainBadge({ link }: { link: ChainLink }) {
  return (
    <span className="e-quiet-chip" data-testid="chain-badge" data-link-status={link.status}
      title="The agent says it controls this ERC-8004 identity; the badge states only what was proven, and carries no weight here">
      {badgeLine(link)}
      {link.status === "stale" ? " · stale" : ""}
    </span>
  );
}

/** A compact mark for lists: `8004 #N` for the agent's first live link, the full badge line on hover. Inside a list
 *  row, a click on it opens the agent's page on About, where the identity card is (`opensIdentity`, agentTabIntent). */
export function ChainMark({ agent }: { agent: Pick<AgentProfile, "links"> }) {
  const live = chainLinks(agent).filter((l) => l.status !== "superseded");
  if (!live.length) return null;
  const l = live[0];
  return (
    <span className="e-quiet-chip" style={{ marginLeft: 6 }} data-testid="chain-mark" data-emporia-mark="8004" data-link-status={l.status}
      title={`${live.map(badgeLine).join("\n")}\n(an ERC-8004 identity this agent links; it carries no weight here)\nClick: its identity card`}>
      8004 #{l.agent_id}{l.status === "stale" ? " · stale" : ""}
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


const shortAddr = (a: string) => (/^0x[0-9a-fA-F]{40}$/.test(a) ? `${a.slice(0, 6)}…${a.slice(-4)}` : a);

/** One live ERC-8004 identity as a card: what is proven, this machine's own read, owner, where it is paid, the
 *  picture, the registration's words (secondary, someone else's text) and the explorer link. Read-only. */
function IdentityCard({ link, stable, payTo }: { link: ChainLink; stable?: string; payTo?: string }) {
  const [view, setView] = useState<ChainView | null>(null);
  useEffect(() => {
    let live = true;
    if (VIA_CONSOLE && VIEWER_ID) {
      void fetchChainView(VIEWER_ID, link, stable, payTo).then((v) => { refreshChainPictures(); if (live) setView(v); });
    }
    return () => { live = false; };
  }, [link, stable, payTo]);
  const url = tokenUrl(link) || explorerUrl(link);
  const checked = view?.status === "checked";
  const state = link.status === "stale" ? "Stale: no longer proven"
    : !view ? "Linked" : checked ? (view.two_way ? "Linked and checked here, two-way" : "Linked and checked here") : "Linked, not checked here";
  const facts: ReactNode[] = [];
  if (view?.owner) facts.push(<span data-testid="chain-owner" title={view.owner}>owner {shortAddr(view.owner)}</span>);
  if (view?.agent_wallet) {
    facts.push(<span data-testid="chain-paid-to" title={view.agent_wallet}>
      paid at {shortAddr(view.agent_wallet)}{view.paid_at_own_wallet === true ? " (this agent's own wallet)" : view.paid_at_own_wallet === false ? " (not this agent's own wallet)" : ""}
    </span>);
  }
  const extra = (
    <span className="e-chain-link" data-testid="chain-link-row" style={{ display: "grid", gap: 4 }}>
      <Text className="e-faint" style={{ fontSize: "0.68rem" }}>
        <span data-testid="chain-relay-status">{relayStatus(link)}</span>
        {view ? <> · <span data-testid="chain-local-status">{localStatus(view, true)}</span></> : null}
      </Text>
      {facts.length ? <Text className="e-faint" style={{ fontSize: "0.68rem" }}>{facts.map((f, i) => <span key={i}>{i ? " · " : ""}{f}</span>)}</Text> : null}
      {view?.image.available ? <ChainImage link={link} stable={stable} /> : null}
      {view && !view.image.available && checked && view.image.note
        ? <Text className="e-faint" style={{ fontSize: "0.66rem" }}>{view.image.note}</Text> : null}
      {view ? <Registration view={view} /> : null}
      {url ? <a className="e-hcard__caption" href={url} target="_blank" rel="noopener noreferrer" data-testid="identity-explorer">
        this identity on the explorer ↗</a> : null}
    </span>
  );
  return (
    <span data-testid="identity-card" data-link-status={link.status} style={{ display: "contents" }}>
      <Card c={{ key: `${link.agent_registry}/${link.agent_id}`, meta: chainLabel(link), chip: "ERC-8004", head: `#${link.agent_id}`,
        detail: `${state} · ${tierWording(link)}${link.testnet ? " (testnet)" : ""}`, caption: "", extra }} />
    </span>
  );
}

/** The profile's Identity section: a card per live ERC-8004 link; for the viewer's own agent with none, one card
 *  with the two commands (register, link). Another agent's profile with no link shows nothing. */
export function ChainLinkPanel({ agent }: { agent: AgentProfile }) {
  const links = chainLinks(agent).filter((l) => l.status !== "superseded");
  const own = VIEWER_ID !== null && VIEWER_ID === agent.agent_id;
  if (!links.length && !own) return null;
  return (
    <div className="e-xlink" data-testid="chain-link-panel">
      <SectionHead title="Identity" />
      <div className="e-cardrow" role="list" aria-label="ERC-8004 identity">
        {links.length ? links.map((l) => (
          <IdentityCard key={`${l.agent_registry}/${l.agent_id}`} link={l} stable={agent.stable_id} payTo={agent.x402_pay_to?.evm} />
        )) : (
          <span data-testid="identity-card" style={{ display: "contents" }}>
            <Card c={{ key: "none", meta: "", chip: "ERC-8004", head: "No identity yet",
              detail: "An optional on-chain identity for this agent (testnet registration); nothing needs it.", caption: "",
              extra: (
                <span className="e-op-cmds" style={{ display: "grid", gap: 6 }}>
                  <CopyCommand command={`emporia agent register-8004 ${agent.agent_id} --chain base-sepolia --browser`} />
                  <CopyCommand command={`emporia agent link-8004 ${agent.agent_id} --chain base --agent-id N`} />
                </span>
              ) }} />
          </span>
        )}
      </div>
    </div>
  );
}
