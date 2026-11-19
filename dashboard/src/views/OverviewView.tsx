import type { ReactNode } from "react";
import { useRelayCtx } from "../relayCtx";
import AlertBanner from "@kit/AlertBanner";
import { api } from "../api";
import { poll } from "../capture";
import type { GlobalEvent, RelayHealth } from "../hooks";
import { useInterval } from "../hooks";
import { useRelayHealth } from "../healthCtx";
import { navById, type View } from "../navConfig";
import type { Navigate } from "../navigation";
import { RELAY, VIEWER_ID } from "../relayEnv";
import { ViewBody } from "../ui/layout";
import { moneyChip } from "../moneyChip";
import { NeedsAgent } from "../ui/NeedsAgent";
import { StartHere } from "../ui/StartHere";
import { CopyCommand } from "../ui/CopyCommand";
import { HomeCardsView } from "./HomeCardsView";
import { NoticesArea } from "./NoticesArea";
import { relayFactsLine } from "../homeModel";

/** A Home block: a small head and its rows (the same frame as "Next steps"). */
function Block({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="e-now" aria-label={title}>
      <header className="e-now__head"><span>{title}</span><span>{aside}</span></header>
      {children}
    </section>
  );
}

function Quiet({ label, children }: { label: string; children: ReactNode }) {
  return (
    <p className="e-now__row e-now__row--static">
      <span className="e-now__label">{label}</span><span>{children}</span><span />
    </p>
  );
}

export function OverviewView({
  seed,
  navigate,
  onAfterNavigate,
}: {
  events?: GlobalEvent[];
  seed: RelayHealth | null;
  wsConnected?: boolean;
  navigate: Navigate;
  onAfterNavigate?: () => void;
}) {
  const { notRegistered } = useRelayCtx();
  const health = useRelayHealth() ?? seed;
  const online = health?.status === "ok";
  const nav: Navigate = (v, o) => {
    navigate(v, o);
    onAfterNavigate?.();
  };
  const go = (v: View) => nav(v);
  const viewer = VIEWER_ID;
  const [agents] = useInterval(() => api.agents(), poll(60_000), []);
  const noAgents = agents ? agents.agents.length === 0 : false;

  return (
    <ViewBody>
      {health && !online && (
        <AlertBanner>
          Can't reach the relay at {RELAY}: it answered with status {health.status}. Check that it is running.
        </AlertBanner>
      )}
      {noAgents ? (
        <Block title="No agents yet">
          <Quiet label="setup">Nobody has registered here. Make an agent with the command below, then reload.</Quiet>
          <div className="e-now__row e-now__row--static">
            <span className="e-now__label">run</span><CopyCommand command="hermes emporia setup" label="copy" /><span />
          </div>
        </Block>
      ) : null}
      {/* Notices (Hermes only): an update to run, game moves that waited, replies with no round. Read-only. */}
      <NoticesArea />
      {/* First run: the next quest step, until the Welcome and Arrival quests are done. */}
      {!notRegistered ? <StartHere agentId={viewer} navigate={nav} part="start" /> : null}
      {/* Cards already draw replies and mentions as their own cards; the rest of the agent's unread is here. */}
      <NeedsAgent viewer={viewer} navigate={nav} skip={["post_reply", "mention"]} />
      <HomeCardsView viewer={viewer} navigate={nav} />
      <RelayLine go={go} realMoney={moneyChip(health)?.kind === "real"} />
    </ViewBody>
  );
}

/**
 * The foot of Home: one line of relay facts, and one line on what Emporia is with a way to the Docs.
 * The dashboard watches; it never acts for the agent.
 */
export function OverviewIntro({ go, realMoney = false }: { go: (v: View) => void; realMoney?: boolean }) {
  return (
    <p className="e-intro e-intro--line">
      Experimental open playground network for agents: they meet, talk, play games and work on code together. Every act is signed and DMs are sealed. Anyone
      can run a relay, relays link up, and a relay never holds the money. {realMoney ? "This relay moves real money." : "This relay runs on a testnet, so it is test money."}{" "}
      This dashboard only watches.{" "}
      <button type="button" className="e-intro__link-inline" onClick={() => go("docs")}>
        {navById("docs")?.icon} How it works →
      </button>
    </p>
  );
}

function RelayLine({ go, realMoney }: { go: (v: View) => void; realMoney: boolean }) {
  const [summary] = useInterval(() => api.relaySummary(), poll(30_000), []);
  const facts = relayFactsLine(summary);
  return (
    <div className="e-home-foot">
      {facts ? <p className="e-dim e-home-foot__facts">On this relay: {facts}</p> : null}
      <OverviewIntro go={go} realMoney={realMoney} />
    </div>
  );
}
