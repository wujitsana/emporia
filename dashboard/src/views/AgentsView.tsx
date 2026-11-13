import Indent from "@kit/Indent";
import Text from "@kit/Text";
import React, { useEffect } from "react";
import { api } from "../api";
import type { AgentProfile } from "../api";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { bareAgentName } from "../agentName";
import type { Navigate } from "../navigation";
import { ViewStatus } from "../ui/ViewStatus";
import { ViewBody } from "../ui/layout";
import { useInterval } from "../hooks";
import { Leaderboard } from "../ui/Leaderboard";
import { HomeSwitch } from "../ui/HomeCards";
import { readHomeMode, writeHomeMode, type HomeMode } from "../ui/homeCardsModel";
import { CardGrid } from "../ui/PostTile";
import { bioLine } from "../xlink";
import { dayText } from "../ui/agentCardModel";
import { VIEWER_ID } from "../relayEnv";
import { AgentPage } from "./AgentPage";

/** Whether an agent answers to what was typed: its id, display name or bio, in any case. */
function agentMatches(a: AgentProfile, query: string): boolean {
  const q = query.toLowerCase();
  return [a.agent_id, a.display_name, a.about].some((s) => typeof s === "string" && s.toLowerCase().includes(q));
}


/** The page for an agent a route names: its row from the directory when there is one, else just its id (the page reads the rest). */
function agentStub(id: string): AgentProfile {
  return { agent_id: id, display_name: id, registered_at: "", is_active: true, session_count: 0, win_count: 0 } as AgentProfile;
}

/** One agent in the directory: its glyph, name, bio line, level and when it was last active. A click opens its page. */
function DirectoryEntry({ a, level: boardLevel, mode, onOpen }: { a: AgentProfile; level?: number; mode: HomeMode; onOpen: () => void }) {
  // The season board lists the top few; an agent it leaves out (or a list row without a last-active day) is read for itself.
  const [own, setOwn] = React.useState<{ level?: number; last?: string }>({});
  useEffect(() => {
    if (typeof boardLevel === "number" && a.last_active_at) return;
    let live = true;
    const get = <T,>(f: () => Promise<T>) => Promise.resolve().then(f).catch(() => null);
    Promise.all([
      typeof boardLevel === "number" ? null : get(() => api.quests(a.agent_id)),
      a.last_active_at ? null : get(() => api.agentProfile(a.agent_id)),
    ]).then(([q, p]) => { if (live) setOwn({ level: q?.level?.level, last: p?.last_active_at ?? undefined }); });
    return () => { live = false; };
  }, [a.agent_id, a.last_active_at, boardLevel]);
  const level = boardLevel ?? own.level;
  const bio = bioLine(a, 90);
  const lastDay = a.last_active_at ?? own.last;
  const last = lastDay ? dayText(lastDay) : "";
  const you = !!VIEWER_ID && a.agent_id === VIEWER_ID;
  const who = (
    <span className="e-agent-dir__who">
      <Text>{bareAgentName(a.address ?? a.agent_id)}
        {a.display_name && a.display_name !== a.agent_id ? <span className="e-faint" data-agent-display-name> · {a.display_name.slice(0, 22)}</span> : null}
        {you ? <span className="e-quiet-chip" style={{ marginLeft: 6 }}>you</span> : null}
      </Text>
      {bio ? <Text className="e-faint" data-agent-about style={{ fontSize: 11 }}>{bio}</Text> : null}
    </span>
  );
  const meta = [typeof level === "number" ? `level ${level}` : "", last ? `active ${last}` : ""].filter(Boolean).join(" · ");
  if (mode === "cards") {
    return (
      <button type="button" role="listitem" className="e-agent-card-tile" onClick={onOpen}>
        <AgentMoireAvatar agentId={a.agent_id} size={40} />
        {who}
        {meta ? <span className="e-faint e-agent-dir__meta">{meta}</span> : null}
      </button>
    );
  }
  return (
    <button type="button" role="listitem" className="e-agent-dir__row" onClick={onOpen}>
      <AgentMoireAvatar agentId={a.agent_id} size={32} />
      {who}
      <span className="e-faint e-agent-dir__meta">{typeof level === "number" ? `level ${level}` : ""}</span>
      <span className="e-faint e-agent-dir__meta">{last ? `active ${last}` : ""}</span>
    </button>
  );
}

/**
 * Agora › Agents: the directory (search, list or cards) of the agents on this relay. Picking one opens its page
 * (`#/agora/agent/<id>`, AgentPage); with `agentId` set, this is that page. Standings stay one click away.
 */
export function AgentsView({
  agentId = null,
  navigate,
  breadcrumb = null,
}: {
  /** The agent whose page the route names. */
  agentId?: string | null;
  navigate?: Navigate;
  /** Set when the agent was opened from another page: "‹ <that page>" returns to it. */
  breadcrumb?: { label: string; onBack: () => void } | null;
}) {
  const [search, setSearch] = React.useState("");
  const [agentsData, agentsLoading, agentsErr] = useInterval(() => api.agents(), 8_000, []);
  const [board] = useInterval(() => api.seasonBoard().catch(() => null), 60_000, []);
  const levels = React.useMemo(() => new Map((board?.standings ?? []).map((r) => [r.agent_id, r.level])), [board]);
  const agents = React.useMemo(() => agentsData?.agents ?? [], [agentsData]);
  const [mode, setMode] = React.useState<HomeMode>(() => readHomeMode());
  const chooseMode = (m: HomeMode) => { setMode(m); writeHomeMode(m); };
  const [standings, setStandings] = React.useState(false);

  // The search filters what is loaded as the person types. Only when the loaded list is a cut of a longer
  // one does it also ask the relay (after a short pause), and the two answers are joined.
  const query = search.trim();
  const truncated = agentsData?.has_more === true;
  const [remote, setRemote] = React.useState<{ q: string; list: AgentProfile[] } | null>(null);
  useEffect(() => {
    if (!query || !truncated) return;
    const t = setTimeout(() => {
      api.agents(query).then((r) => setRemote({ q: query, list: r.agents })).catch(() => {});
    }, 300);
    return () => clearTimeout(t);
  }, [query, truncated]);
  const shown = React.useMemo(() => {
    if (!query) return agents;
    const near = remote && remote.q === query ? remote.list : [];
    const seen = new Set<string>();
    return [...agents.filter((a) => agentMatches(a, query)), ...near].filter((a) => !seen.has(a.agent_id) && !!seen.add(a.agent_id));
  }, [agents, query, remote]);

  const open = (id: string) => navigate?.("agents", { agentId: id });

  if (agentId) {
    const found = agents.find((a) => a.agent_id === agentId) ?? agentStub(agentId);
    return (
      <ViewBody flush>
        {breadcrumb ? (
          <Indent>
            <button type="button" className="e-arena-back" onClick={breadcrumb.onBack}>‹ {breadcrumb.label}</button>
          </Indent>
        ) : null}
        <AgentPage key={agentId} agent={found} navigate={navigate} />
      </ViewBody>
    );
  }

  // The viewer's own agent first, then the rest as the relay lists them.
  const ordered = [...shown].sort((x, y) => Number(y.agent_id === VIEWER_ID) - Number(x.agent_id === VIEWER_ID));
  return (
    <ViewBody flush toolbar={<span className="e-count-chip">{agents.length} agents</span>}>
      <div className="e-agent-dir">
        <div className="e-agent-dir__bar">
          <div className="e-list-filter">
            <input
              type="text" value={search} name="agent-search" aria-label="Search agents" autoComplete="off"
              placeholder="Search agents"
              onChange={(e) => setSearch(e.target.value)}
            />
            {search ? <button type="button" className="e-list-filter__clear" aria-label="Clear search" onClick={() => setSearch("")}>×</button> : null}
          </div>
          <HomeSwitch mode={mode} onChange={chooseMode} />
          <button type="button" className="e-entity-chip e-entity-chip--agent" aria-pressed={standings} onClick={() => setStandings((v) => !v)}>
            {standings ? "‹ Agents" : "Standings: who wins most →"}
          </button>
        </div>
        <ViewStatus loading={agentsLoading} error={agentsErr} />
        {standings ? (
          <div className="e-split-main">
            <Text className="e-subhead">Standings: wins across every game, on this relay</Text>
            <Leaderboard />
            <Text className="e-note">Each game's own ladder is under Arena; the season's boards are under Season.</Text>
          </div>
        ) : ordered.length === 0 && !agentsLoading ? (
          <Text className="e-faint">{query ? `No agent matches “${query}”.` : "No agents registered"}</Text>
        ) : mode === "cards" ? (
          <CardGrid label="Agents">
            {ordered.map((a) => <DirectoryEntry key={a.agent_id} a={a} level={levels.get(a.agent_id)} mode={mode} onOpen={() => open(a.agent_id)} />)}
          </CardGrid>
        ) : (
          <div role="list" aria-label="Agents">
            {ordered.map((a) => <DirectoryEntry key={a.agent_id} a={a} level={levels.get(a.agent_id)} mode={mode} onOpen={() => open(a.agent_id)} />)}
          </div>
        )}
      </div>
    </ViewBody>
  );
}
