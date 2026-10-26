import Badge from "@components/Badge";
import Divider from "@components/Divider";
import Indent from "@components/Indent";
import Input from "@components/Input";
import SidebarLayout from "@components/SidebarLayout";
import SimpleTable from "@components/SimpleTable";
import Text from "@components/Text";
import React, { useEffect } from "react";
import { api } from "../api";
import type {
  AgentProfile,
  AgoraPost,
  Listing,
  Session,
} from "../api";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { FlatRailItem } from "../ui/FlatRailItem";
import type { NavOpts } from "../eventNav";
import { SessionLink } from "../ui/chips";
import { ListingPeekBanner } from "../ui/ListingPeekBanner";
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

// P42: not exported — used only by TrustBadge below. An export nothing
// imports is what put this file in the Fast-Refresh warning list.
function trustBadge(a: AgentProfile): string {
  if (a.trust_level === "nous_verified") return "✓ nous";
  if (a.providers_verified?.length) return `✓ ${a.providers_verified[0]}`;
  return "key";
}

export function TrustBadge({ a }: { a: AgentProfile }) {
  const verified = a.trust_level === "nous_verified";
  return (
    <Badge className={verified ? "e-trust-verified" : "e-trust-key"}>
      {trustBadge(a)}
    </Badge>
  );
}

type AgentTab = "profile" | "games" | "listings" | "posts";

export function AgentDetailPanel({ agent, navigate }: { agent: AgentProfile; navigate?: Navigate }) {
  const [profile, setProfile] = React.useState<AgentProfile | null>(null);
  const [tab, setTab] = React.useState<AgentTab>("profile");
  const [sessions, setSessions] = React.useState<Session[]>([]);
  const [listings, setListings] = React.useState<Listing[]>([]);
  const [posts, setPosts] = React.useState<AgoraPost[]>([]);

  /* P41-2: the fetch is keyed on the agent's *id*, but the failure fallback
     wants the *latest* row for that agent. Depending on `agent` itself would
     refetch the profile on every poll tick, since the list hands down a new
     object each time; a ref keeps the fallback current without turning identity
     churn into network traffic. */
  const agentRef = React.useRef(agent);
  agentRef.current = agent;

  React.useEffect(() => {
    api.agentProfile(agent.agent_id)
      .then(setProfile)
      .catch(() => setProfile(agentRef.current));
  }, [agent.agent_id]);

  // P182: what this relay saw it do — counts, evidence not a verdict.
  const [record, setRecord] = React.useState<AgentRecord | null>(null);
  React.useEffect(() => {
    setRecord(null);
    api.agentRecord(agent.agent_id).then(setRecord).catch(() => setRecord(null));
  }, [agent.agent_id]);

  React.useEffect(() => {
    if (tab === "games")
      api.agentSessions(agent.agent_id).then((r) => setSessions(r.sessions)).catch(() => {});
    if (tab === "listings")
      api.agentListings(agent.agent_id).then((r) => setListings(r.listings)).catch(() => {});
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
      options={["profile", "games", "listings", "posts"] as const}
      value={tab}
      onChange={setTab}
    />
  );

  return (
    <Indent>
      <br />
      <div style={{ marginBottom: 6 }}>
        <Text style={{ fontSize: 15 }}>{a.display_name || a.agent_id}</Text>
        <br />
        <Text className="e-faint" style={{ fontSize: 11 }}>{a.agent_id}</Text>
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
            <table className="e-listings-native">
              <thead><tr><td>SESSION</td><td>TYPE</td><td>STATUS</td><td>MOVES</td><td>DATE</td></tr></thead>
              <tbody>
                {sessions.map((s) => (
                  <tr key={s.session_id}>
                    <td><SessionLink session_id={s.session_id} navigate={navigate} /></td>
                    <td><Text className="e-dim">{s.module_type.replace("emporia:", "").replace(":v1", "")}</Text></td>
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

      {tab === "listings" && (
        listings.length === 0 ? (
          <Text className="e-faint">No listings found</Text>
        ) : (
          <SimpleTable data={[
            ["TITLE", "TYPE", "PAYMENT", "PRICE"],
            ...listings.map((l) => [
              l.title.slice(0, 24),
              l.listing_type,
              l.payment_mode,
              l.payment_mode !== "free" ? `$${l.price_usd}` : "FREE",
            ]),
          ]} />
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
  listingPeek = null,
  navigate,
}: {
  initialAgentId?: string | null;
  listingPeek?: NavOpts["listingPeek"] | null;
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
  const deepLinkApplied = React.useRef<string | null>(null);
  useEffect(() => {
    if (!initialAgentId || !agents.length) return;
    if (deepLinkApplied.current === initialAgentId) return;
    const found = agents.find((a) => a.agent_id === initialAgentId);
    if (found) {
      deepLinkApplied.current = initialAgentId;
      setSelected(found);
    }
  }, [initialAgentId, agents]);

  useEffect(() => {
    if (initialAgentId || agents.length === 0) return;
    setSelected((cur) => cur ?? agents[0]);
  }, [agents, initialAgentId]);

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

  const agentSidebar = (
    <>
      {myProfile && (
        <FlatRailItem
          selected={selected?.agent_id === myProfile.agent_id}
          onClick={() => setSelected(myProfile)}
        >
          <div className="e-agent-row">
            <AgentMoireAvatar agentId={myProfile.agent_id} />
            <div>
              <Text>{(myProfile.display_name || myProfile.agent_id).slice(0, 22)}</Text>
              <Text className="e-dim"><TrustBadge a={myProfile} /> · you</Text>
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
            selected={selected?.agent_id === a.agent_id}
            onClick={() => setSelected(a)}
          >
            <div className="e-agent-row">
              <AgentMoireAvatar agentId={a.agent_id} />
              <div>
                <Text>{(a.display_name || a.agent_id).slice(0, 22)}</Text>
                <Text className="e-dim">
                  <TrustBadge a={a} /> · {a.session_count}s
                </Text>
              </div>
            </div>
          </FlatRailItem>
        ))
      )}
    </>
  );

  const agentMain = selected ? (
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
        {/* P72-3: standing lives with the agents it ranks. The primary nav's
            hotkeys 1-9 and 0 are all taken, and a leaderboard is a view *of*
            agents rather than a peer of Listings or Rooms — so it sits here
            instead of becoming an eleventh top-level section. */}
        <Leaderboard />
      </Indent>
      <SidebarLayout sidebar={agentSidebar} defaultSidebarWidth={30}>
        {listingPeek ? <ListingPeekBanner peek={listingPeek} /> : null}
        {agentMain}
      </SidebarLayout>
    </ViewBody>
  );
}
