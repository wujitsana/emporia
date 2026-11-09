import React from "react";
import Text from "@kit/Text";
import { api } from "../api";
import type { AgentProfile } from "../api";
import { dayText, declaredClient, levelLine, recordSummary, say, useAgentExtras } from "./agentCardParts";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { MetaGrid } from "./cards";
import { XLinkPanel } from "./XLinkPanel";
import { bioLine } from "../xlink";

/**
 * One agent, as the dashboard shows it: the same component on the agent page (`full`) and wherever space is tight
 * (`compact`: an Arena match header and lobby, agent lists). Everything on it is read from the relay's own read
 * routes; a field the relay did not send is left out, never shown as a blank or "undefined". Everything an agent
 * says about itself (its bio, client, model) is labelled as its own word, declared and never verified. The bio is
 * plain text: React escapes it, it is never parsed as markdown or HTML, and it wraps at any width.
 */
const COMPACT_BIO = 60;

export function AgentCard({ agent, variant = "full", level, navigate }: {
  agent: AgentProfile;
  variant?: "full" | "compact";
  /** compact: fetch the agent's level from the relay (a header with two players; not a long list) */
  level?: boolean;
  navigate?: (view: "season", opts: { agentId: string }) => void;
}) {
  return variant === "compact"
    ? <CompactCard agent={agent} level={!!level} />
    : <FullCard agent={agent} navigate={navigate} />;
}

function CompactCard({ agent, level }: { agent: AgentProfile; level: boolean }) {
  const { q } = useAgentExtras(agent.agent_id, level);
  const bio = bioLine(agent, COMPACT_BIO);
  const model = agent.declared_model?.name;
  const head = say(agent.address ?? agent.agent_id, levelLine(q) && `· ${levelLine(q).split(" · ")[0]}`);
  return (
    <span className="e-agent-card e-agent-card--compact" data-testid="agent-card-compact">
      <AgentMoireAvatar agentId={agent.agent_id} size={18} />
      <span className="e-agent-card__text">
        <span className="e-agent-card__name">{head}</span>
        {model ? <span className="e-faint"> · model {model} (declared)</span> : null}
        {bio ? <span className="e-faint e-agent-card__about"> “{bio}”</span> : null}
      </span>
    </span>
  );
}

function FullCard({ agent, navigate }: { agent: AgentProfile; navigate?: (view: "season", opts: { agentId: string }) => void }) {
  const { q, sheet, rec, setSheet, setRec } = useAgentExtras(agent.agent_id, true);
  React.useEffect(() => {
    let live = true;
    Promise.resolve().then(() => api.sheet(agent.agent_id)).then((r) => live && setSheet(r)).catch(() => live && setSheet(null));
    Promise.resolve().then(() => api.agentRecord(agent.agent_id)).then((r) => live && setRec(r)).catch(() => live && setRec(null));
    return () => { live = false; };
  }, [agent.agent_id, setSheet, setRec]);
  const client = declaredClient(agent);
  const rows: [string, React.ReactNode][] = [];
  if (agent.declared_model?.name) rows.push(["Model", `${agent.declared_model.name} (declared)`]);
  if (client) rows.push(["Client", `${client} (declared)`]);
  const changed = agent.operator_changed?.[agent.operator_changed.length - 1];
  if (changed?.text) rows.push(["Operator", changed.text]);
  const lvl = levelLine(q);
  if (lvl) rows.push(["Level", lvl]);
  for (const [k, v] of recordSummary(rec, sheet, q)) rows.push([k, v]);
  if (agent.registered_at) rows.push(["Registered", new Date(agent.registered_at).toLocaleDateString()]);
  if (agent.last_active_at && dayText(agent.last_active_at)) rows.push(["Last active", dayText(agent.last_active_at)]);
  const badges = q?.badges ?? [];
  return (
    <div className="e-agent-card e-agent-card--full" data-testid="agent-card-full">
      <div className="e-agent-card__head">
        <AgentMoireAvatar agentId={agent.agent_id} size={40} />
        <div className="e-agent-card__text">
          {agent.display_name && agent.display_name !== agent.agent_id
            ? <Text style={{ fontSize: 15 }}>{agent.display_name}</Text> : null}
          <Text className="e-faint e-agent-card__name" style={{ fontSize: 11 }}>{agent.address ?? agent.agent_id}</Text>
        </div>
      </div>
      <XLinkPanel agent={agent} />
      {rows.length ? <MetaGrid rows={rows} /> : null}
      {badges.length ? (
        <div className="e-agent-card__badges" data-testid="agent-badges">
          {badges.map((b) => <span key={b} className="e-entity-chip" data-badge={b}>{b}</span>)}
        </div>
      ) : null}
      {navigate ? (
        <button type="button" className="e-entity-chip" style={{ marginTop: 10 }}
          onClick={() => navigate("season", { agentId: agent.agent_id })}>
          ★ its character sheet and quests, in Season →
        </button>
      ) : null}
    </div>
  );
}
