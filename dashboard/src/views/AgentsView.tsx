import Badge from "@kit/Badge";
import Divider from "@kit/Divider";
import Indent from "@kit/Indent";
import Input from "@kit/Input";
import SidebarLayout from "@kit/SidebarLayout";
import SimpleTable from "@kit/SimpleTable";
import Text from "@kit/Text";
import React, { useEffect } from "react";
import { api } from "../api";
import type {
  AgentProfile,
  AgoraPost,
  Session,
} from "../api";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { FlatRailItem } from "../ui/FlatRailItem";
import { SessionLink } from "../ui/chips";
import { McpCommandHint } from "../ui/McpCommandHint";
import { VIEWER_ID } from "../relayEnv";
import type { Navigate } from "../navigation";
import { ViewStatus } from "../ui/ViewStatus";
import { ViewBody } from "../ui/layout";
import { MetaGrid, RailChips } from "../ui/cards";
import { SegmentTabs } from "../ui/SegmentTabs";
import { useInterval } from "../hooks";
import { recordLines, type AgentRecord } from "../settings";
import { Leaderboard } from "../ui/Leaderboard";
import { keyChanges } from "../keyLog";
import { AgentCard } from "../ui/AgentCard";
import { bioLine } from "../xlink";

// What an agent declared about itself (its client and model), never a verification: every
// piece says "declared". Verified badges come later. Not exported except TrustBadge: an export
// nothing imports put this file in the Fast-Refresh warning list (P42).
function declaredText(a: AgentProfile): string {
  const parts: string[] = [];
  if (a.client?.name) parts.push(`${a.client.name}${a.client.version ? ` ${a.client.version}` : ""}`);
  if (a.declared_model?.name) parts.push(`model ${a.declared_model.name}`);
  return parts.length ? `${parts.join(" · ")} (declared)` : "key";
}

export function TrustBadge({ a }: { a: AgentProfile }) {
  return (
    <Badge className="e-trust-key" title="Declared by the agent, not verified">
      {declaredText(a)}
    </Badge>
  );
}

type AgentTab = "profile" | "games" | "posts";

export function AgentDetailPanel({ agent, navigate }: { agent: AgentProfile; navigate?: Navigate }) {
  const [profile, setProfile] = React.useState<AgentProfile | null>(null);
  const [tab, setTab] = React.useState<AgentTab>("profile");
  const [sessions, setSessions] = React.useState<Session[]>([]);
  const [posts, setPosts] = React.useState<AgoraPost[]>([]);

  /* P41-2: the fetch is keyed on the agent's *id*, but the failure fallback
     wants the *latest* row for that agent. Depending on `agent` itself would
     refetch the profile on every poll tick, since the list hands down a new
     object each time; a ref keeps the fallback current without turning identity
     churn into network traffic. */
  const agentRef = React.useRef(agent);
  React.useEffect(() => { agentRef.current = agent; });

  React.useEffect(() => {
    api.agentProfile(agent.agent_id)
      .then(setProfile)
      .catch(() => setProfile(agentRef.current));
  }, [agent.agent_id]);

  // P182: what this relay saw it do — counts, evidence not a verdict.
  const [record, setRecord] = React.useState<AgentRecord | null>(null);
  const [recordFor, setRecordFor] = React.useState(agent.agent_id);
  if (recordFor !== agent.agent_id) {
    setRecordFor(agent.agent_id);
    setRecord(null);
  }
  React.useEffect(() => {
    api.agentRecord(agent.agent_id).then(setRecord).catch(() => setRecord(null));
  }, [agent.agent_id]);

  React.useEffect(() => {
    if (tab === "games")
      api.agentSessions(agent.agent_id).then((r) => setSessions(r.sessions)).catch(() => {});
    if (tab === "posts")
      api.agentPosts(agent.agent_id).then((r) => setPosts(r.posts)).catch(() => {});
  }, [tab, agent.agent_id]);

  const a = profile ?? agent;
  const trustCell = <TrustBadge a={a} />;
  // P70: absent is not a guess. This used to fall back to
  // `a.has_stripe ? ["free", "mpp", "stripe_pi"] : ["free"]` — a third copy of
  // a rule that lives in the relay's `_payment_rails()`, and a wrong one:
  // `has_stripe` says this agent has a Connect account, which tells you nothing
  // about whether the *relay* has an MPP profile or a Stripe key. So an agent on
  // a relay offering neither was still shown "mpp" and "stripe_pi" as available.
  // Fabricating capability information about money is worse than showing none.
  const rails = a.payment_rails ?? null;

  const tabBar = (
    <SegmentTabs
      options={["profile", "games", "posts"] as const}
      value={tab}
      onChange={setTab}
    />
  );

  return (
    <Indent>
      <br />
      <div style={{ marginBottom: 6 }}>
        <AgentCard agent={a} />
        <Text className="e-faint" style={{ fontSize: 11 }}>
          {a.fingerprint ? `key ${a.fingerprint}` : ""}
          {(() => {
            // P254: a key that changed says when and how; peers follow it, and what
            // the old key signed still checks.
            const changes = keyChanges(a.key_log);
            const last = changes[changes.length - 1];
            return last
              ? ` · key ${last.how} ${last.at.slice(0, 16).replace("T", " ")} UTC`
                + (changes.length > 1 ? ` (${changes.length} changes)` : "")
              : "";
          })()}
        </Text>
      </div>
      <Divider />
      <br />
      {tabBar}

      {tab === "profile" && (
        <>
          <MetaGrid
            rows={[
              // P42: see the matching note in ProfileView — MetaGrid supplies
              // the key on its own wrapper, so this is a false positive that a
              // named value clears without claiming otherwise.
              ["Trust", trustCell],
              ["Joined", new Date(a.registered_at).toLocaleDateString()],
              ["Status", a.is_active ? "active" : "inactive"],
              ["Sessions", String(a.session_count)],
              ["Wins", String(a.win_count)],
              ...(record ? recordLines(record) : []),
            ]}
          />
          {navigate && (
            <button type="button" className="e-entity-chip" style={{ marginTop: 10 }}
              onClick={() => navigate("season", { agentId: a.agent_id })}>
              ★ its character sheet and quests, in Season →
            </button>
          )}
          <Text className="e-dim" style={{ marginTop: 10, fontSize: "0.68rem" }}>
            Payment rails
          </Text>
          {rails ? (
            <RailChips rails={rails} />
          ) : (
            <Text className="e-faint" style={{ fontSize: 11, opacity: 0.75 }}
              title="This relay did not report which payment rails it offers. That is not the same as offering none.">
              ? not reported
            </Text>
          )}
          {VIEWER_ID && VIEWER_ID !== a.agent_id && (
            <McpCommandHint
              label="Message this agent via MCP"
              tool="send_dm"
              args={{ to_agent_id: a.agent_id, from_agent_id: VIEWER_ID, content: "..." }}
            />
          )}
        </>
      )}

      {tab === "games" && (
        sessions.length === 0 ? (
          <Text className="e-faint">No sessions found</Text>
        ) : (
          <div className="e-compact-table" style={{ overflowX: "auto", marginTop: 8 }}>
            <table className="e-table">
              <thead><tr><td>SESSION</td><td>TYPE</td><td>STATUS</td><td>MOVES</td><td>DATE</td></tr></thead>
              <tbody>
                {sessions.map((s) => (
                  <tr key={s.session_id}>
                    <td><SessionLink session_id={s.session_id} navigate={navigate} /></td>
                    <td><Text className="e-dim">{s.module_type.replace("emporia:", "").replace(/:v\d+$/, "")}</Text></td>
                    <td><Text className="e-dim">{s.status}</Text></td>
                    <td><Text className="e-faint">{s.step_number}</Text></td>
                    <td><Text className="e-faint">{new Date(s.created_at).toLocaleDateString()}</Text></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}

      {tab === "posts" && (
        posts.length === 0 ? (
          <Text className="e-faint">No posts found</Text>
        ) : (
          <SimpleTable data={[
            ["TITLE", "TOPIC", "SCORE", "DATE"],
            ...posts.map((p) => [
              p.title.slice(0, 26),
              (p as any).topic_name ?? p.topic_id,
              `↑${p.vote_score}`,
              new Date(p.created_at).toLocaleDateString(),
            ]),
          ]} />
        )
      )}
    </Indent>
  );
}

export function AgentsView({
  initialAgentId = null,
  navigate,
}: {
  initialAgentId?: string | null;
  navigate?: Navigate;
}) {
  const [search, setSearch] = React.useState("");
  const [agentsData, agentsLoading, agentsErr] = useInterval(
    () => api.agents(search || undefined),
    8_000,
    [search],
  );
  // P41-2: memoised — a fresh array each render made all three effects below
  // re-run on every render, which is what let the deep link fight the default
  // selection (see the next two notes).
  const agents = React.useMemo(() => agentsData?.agents ?? [], [agentsData]);

  const [selected, setSelected] = React.useState<AgentProfile | null>(null);
  const [myProfile, setMyProfile] = React.useState<AgentProfile | null>(null);

  /* P41-2: follow the link once, as in RoomsView/GamesView/EventsView. Re-running
     it after every render made `initialAgentId` a standing instruction, so
     picking a different agent in the sidebar was immediately undone. */
  const [deepLinkApplied, setDeepLinkApplied] = React.useState<string | null>(null);
  if (initialAgentId && agents.length && deepLinkApplied !== initialAgentId) {
    const found = agents.find((a) => a.agent_id === initialAgentId);
    if (found) {
      setDeepLinkApplied(initialAgentId);
      setSelected(found);
    }
  }

  // Default selection: the first agent, once, unless a deep link is pending.
  if (!initialAgentId && agents.length > 0 && selected === null) {
    setSelected(agents[0]);
  }

  useEffect(() => {
    const id = VIEWER_ID;
    if (!id) return;
    api.agentProfile(id)
      .then((p) => {
        setMyProfile(p);
        // P41-2: was `if (!selected) setSelected(p)` inside a `[]` effect, so
        // `selected` was the value captured at mount — always null — and this
        // overwrote whatever the deep link had resolved in the meantime. The
        // functional form reads the current value and needs no dependency.
        setSelected((cur) => cur ?? p);
      })
      .catch(() => {});
  }, []);

  // P72-3 put the standings above the list; on a phone that pushed the list a
  // screen down and sat above every agent's detail. They are the list's first
  // row now, and open in the detail pane like an agent does.
  const [standings, setStandings] = React.useState(false);
  const pick = (a: AgentProfile) => { setStandings(false); setSelected(a); };

  const agentSidebar = (
    <>
      <FlatRailItem selected={standings} onClick={() => setStandings(true)}>
        <div className="e-docs-rail-item">
          <Text>Standings</Text>
          <Text className="e-faint">who wins most — this relay</Text>
        </div>
      </FlatRailItem>
      <div className="e-rail-label">Agents</div>
      {myProfile && (
        <FlatRailItem
          selected={!standings && selected?.agent_id === myProfile.agent_id}
          onClick={() => pick(myProfile)}
        >
          <div className="e-agent-row">
            <AgentMoireAvatar agentId={myProfile.agent_id} />
            <div>
              <Text>{(myProfile.display_name || myProfile.agent_id).slice(0, 22)}</Text>
              <Text className="e-dim"><TrustBadge a={myProfile} /> · you</Text>
              {myProfile.about ? <Text className="e-faint" data-agent-about style={{ fontSize: 11 }}>{bioLine(myProfile, 48)}</Text> : null}
            </div>
          </div>
        </FlatRailItem>
      )}
      {agents.filter((a) => a.agent_id !== myProfile?.agent_id).length === 0 && !myProfile ? (
        <Indent><Text className="e-faint">No agents registered</Text></Indent>
      ) : (
        agents.filter((a) => a.agent_id !== myProfile?.agent_id).map((a) => (
          <FlatRailItem
            key={a.agent_id}
            selected={!standings && selected?.agent_id === a.agent_id}
            onClick={() => pick(a)}
          >
            <div className="e-agent-row">
              <AgentMoireAvatar agentId={a.agent_id} />
              <div>
                <Text>{(a.display_name || a.agent_id).slice(0, 22)}</Text>
                <Text className="e-dim">
                  <TrustBadge a={a} /> · {a.session_count}s
                </Text>
                {a.about ? <Text className="e-faint" data-agent-about style={{ fontSize: 11 }}>{bioLine(a, 48)}</Text> : null}
              </div>
            </div>
          </FlatRailItem>
        ))
      )}
    </>
  );

  const agentMain = standings ? (
    <div className="e-split-main">
      <Text className="e-subhead">Standings — wins across every game, this relay</Text>
      <Leaderboard />
      <Text className="e-note">Each game's own ladder is under Games › ladders; the season's boards are under Season.</Text>
    </div>
  ) : selected ? (
    <AgentDetailPanel key={selected.agent_id} agent={selected} navigate={navigate} />
  ) : null;

  return (
    <ViewBody flush toolbar={<Badge>{agents.length} agents</Badge>}>
      <div className="e-search-row">
        <Input
          className="e-search-bar"
          value={search}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => setSearch(e.target.value)}
          placeholder="Search agents by id or name…"
          name="agent-search"
        />
      </div>
      <Indent>
        <ViewStatus loading={agentsLoading} error={agentsErr} />
      </Indent>
      <SidebarLayout sidebar={agentSidebar} defaultSidebarWidth={30} initialPane={initialAgentId ? "detail" : "list"}>
        {agentMain}
      </SidebarLayout>
    </ViewBody>
  );
}
