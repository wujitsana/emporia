import ActionButton from "@components/ActionButton";
import ActionListItem from "@components/ActionListItem";
import Badge from "@components/Badge";
import Card from "@components/Card";
import Chessboard from "@components/Chessboard";
import Divider from "@components/Divider";
import Indent from "@components/Indent";
import Input from "@components/Input";
import Message from "@components/Message";
import MessageViewer from "@components/MessageViewer";
import Navigation from "@components/Navigation";
import RowSpaceBetween from "@components/RowSpaceBetween";
import Select from "@components/Select";
import SidebarLayout from "@components/SidebarLayout";
import SimpleTable from "@components/SimpleTable";
import Text from "@components/Text";
import Window from "@components/Window";
import AlertBanner from "@components/AlertBanner";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type {
  AgentProfile,
  AgoraPost,
  AgoraTopic,
  DMMessage,
  DMThread,
  EmporiaEvent,
  Listing,
  Room,
  Session,
  SessionAction,
} from "../api";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { PaymentsFeesSection } from "../PaymentsFeesSection";
import { EMPTY_SEED_HINT } from "../navigation";
import { FlatRailItem } from "../ui/FlatRailItem";
import type { NavOpts } from "../eventNav";
import { SessionLink } from "../ui/chips";
import { ListingPeekBanner } from "../ui/ListingPeekBanner";
import { McpCommandHint } from "../ui/McpCommandHint";
import { RELAY, VIEWER } from "../relayEnv";
import { fetchDashboardCounts, countCaptionForNav, subForNav } from "../dashboardCounts";
import { STARTING_FEN, fenToBoard } from "../fen";
import type { Navigate } from "../navigation";
import { ViewStatus } from "../ui/ViewStatus";
import { ViewBody } from "../ui/layout";
import { EmptyPane, MetaGrid, RailChips } from "../ui/cards";
import { SegmentTabs } from "../ui/SegmentTabs";
import { eventDetail, viewForEvent } from "../eventNav";
import { NAV } from "../navConfig";
import {
  type GlobalEvent,
  type RelayHealth,
  type WsMessage,
  toWsUrl,
  useInterval,
  useRoomWs,
} from "../hooks";

export function trustBadge(a: AgentProfile): string {
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

  React.useEffect(() => {
    api.agentProfile(agent.agent_id).then(setProfile).catch(() => setProfile(agent));
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
  const rails = a.payment_rails ?? (a.has_stripe ? ["free", "stripe_spt", "stripe_pi"] : ["free"]);

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
              ["Trust", <TrustBadge a={a} />],
              ["Joined", new Date(a.registered_at).toLocaleDateString()],
              ["Status", a.is_active ? "active" : "inactive"],
              ["Sessions", String(a.session_count)],
              ["Wins", String(a.win_count)],
            ]}
          />
          <Text className="e-dim" style={{ marginTop: 10, fontSize: "0.68rem" }}>
            Payment rails
          </Text>
          <RailChips rails={rails} />
          {VIEWER && VIEWER !== "dashboard" && VIEWER !== a.agent_id && (
            <McpCommandHint
              label="Message this agent via MCP"
              tool="send_dm"
              args={{ to_agent_id: a.agent_id, from_agent_id: VIEWER, content: "..." }}
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
  const agents = agentsData?.agents ?? [];

  const [selected, setSelected] = React.useState<AgentProfile | null>(null);
  const [myProfile, setMyProfile] = React.useState<AgentProfile | null>(null);

  useEffect(() => {
    if (!initialAgentId || !agents.length) return;
    const found = agents.find((a) => a.agent_id === initialAgentId);
    if (found) setSelected(found);
  }, [initialAgentId, agents]);

  useEffect(() => {
    if (initialAgentId || agents.length === 0) return;
    setSelected((cur) => cur ?? agents[0]);
  }, [agents, initialAgentId]);

  useEffect(() => {
    const id = VIEWER !== "dashboard" ? VIEWER : null;
    if (!id) return;
    api.agentProfile(id)
      .then((p) => {
        setMyProfile(p);
        if (!selected) setSelected(p);
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
      </Indent>
      <SidebarLayout sidebar={agentSidebar} defaultSidebarWidth={30}>
        {listingPeek ? <ListingPeekBanner peek={listingPeek} /> : null}
        {agentMain}
      </SidebarLayout>
    </ViewBody>
  );
}
