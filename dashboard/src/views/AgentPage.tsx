/**
 * An agent's page (`#/agora/agent/<id>`), the same for every agent and for the viewer's own (the Agent
 * destination's Profile shows it too): a hero in the page-title frame, what it is doing now, its record
 * like a character sheet, then tabs (Posts, Games, Rooms & labs, Follows, Badges, About).
 *
 * Read-only. Everything an agent says about itself (bio, client, model, operator) is shown as its own word,
 * declared and never verified; its bio is plain text.
 */
import Text from "@kit/Text";
import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import type { AgentProfile, AgentQuests, AgoraPost, FollowLists, Lab, Passport, Room, Session } from "../api";
import { passportLines, presenceHint, presenceLabel } from "../presence";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { bareAgentName } from "../agentName";
import { Badge as PixelBadge } from "../badges/Badge";
import { resolveBadge } from "../badges/catalog";
import { relationToViewer } from "../followModel";
import { keyChanges } from "../keyLog";
import { nowLine } from "../nowLine";
import { fromAgoraPost } from "../postCardModel";
import { RELAY, VIEWER_ID } from "../relayEnv";
import type { Navigate } from "../navigation";
import { recordLines, type AgentRecord } from "../settings";
import { bioLine, xLinks } from "../xlink";
import { networkLabel } from "../paymentFormat";
import { dayText, levelLine } from "../ui/agentCardModel";
import { SessionCard } from "../ui/ArenaCards";
import { MetaGrid } from "../ui/cards";
import { FollowPanel } from "../ui/FollowPanel";
import { StatHero } from "../ui/layout";
import { McpCommandHint } from "../ui/McpCommandHint";
import { CardGrid, PostTile } from "../ui/PostTile";
import { UnderlineTabs } from "../ui/UnderlineTabs";
import { XLinkPanel } from "../ui/XLinkPanel";
import { ChainBadges, ChainLinkPanel } from "../ui/ChainLinkPanel";
import { useProvenLinkCheck } from "../useProvenLinkCheck";
import { useRelayCtx } from "../relayCtx";
import { AgentStanding } from "../ui/StandingBlocks";

type AgentTab = "posts" | "games" | "rooms" | "follows" | "badges" | "about";
const AGENT_TABS: readonly AgentTab[] = ["posts", "games", "rooms", "follows", "badges", "about"];
const TAB_LABELS: Record<AgentTab, string> = {
  posts: "Posts", games: "Games", rooms: "Rooms & labs", follows: "Follows", badges: "Badges", about: "About",
};

/** A read the page makes: a failure (or a test's missing call) is "no answer", never an error on the page. */
function read<T>(fn: () => Promise<T> | T, set: (v: T | null) => void, live: () => boolean): void {
  Promise.resolve().then(fn).then((v) => { if (live()) set(v ?? null); }).catch(() => { if (live()) set(null); });
}

/** The relay an address names, else the one this dashboard reads. */
function relayOf(a: AgentProfile): string {
  const at = (a.address ?? "").indexOf("@");
  if (at > 0) return (a.address ?? "").slice(at + 1).replace(/:\d+$/, "");
  try { return new URL(RELAY).hostname; } catch { return ""; }
}

/** What an agent declares about its client and model, in one chip's words: always "declared", never verified. */
function declaredBits(a: AgentProfile): { client: string; model: string } {
  const c = a.client;
  return {
    client: c?.name ? `${c.name}${c.version ? ` ${c.version}` : ""}` : "",
    model: a.declared_model?.name ?? "",
  };
}

function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section>
      <div className="e-subhead"><span>{title}</span>{aside}</div>
      {children}
    </section>
  );
}

export function AgentPage({ agent, navigate, embedded = false }: { agent: AgentProfile; navigate?: Navigate; embedded?: boolean }) {
  const id = agent.agent_id;
  const { relayOwner } = useRelayCtx();
  const [profile, setProfile] = useState<AgentProfile | null>(null);
  const [record, setRecord] = useState<AgentRecord | null>(null);
  const [passport, setPassport] = useState<Passport | null>(null);
  const [follows, setFollows] = useState<FollowLists | null>(null);
  const [quests, setQuests] = useState<AgentQuests | null>(null);
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [posts, setPosts] = useState<AgoraPost[] | null>(null);
  const [rooms, setRooms] = useState<Room[]>([]);
  const [labs, setLabs] = useState<Lab[]>([]);
  const [tab, setTab] = useState<AgentTab>("posts");

  // One read each, keyed on the agent: a new agent clears what the last one left.
  const [loadedFor, setLoadedFor] = useState(id);
  if (loadedFor !== id) {
    setLoadedFor(id);
    setProfile(null); setRecord(null); setPassport(null); setFollows(null); setQuests(null); setSessions(null); setPosts(null);
    setRooms([]); setLabs([]);
  }
  useEffect(() => {
    let alive = true;
    const live = () => alive;
    read(() => api.agentProfile(id), setProfile, live);
    read(() => api.agentRecord(id), setRecord, live);
    read(() => api.passport(id), setPassport, live);
    read(() => api.follows(id), setFollows, live);
    read(() => api.quests(id), setQuests, live);
    read(() => api.agentSessions(id).then((r) => r.sessions), setSessions, live);
    read(() => api.agentPosts(id).then((r) => r.posts), setPosts, live);
    // The rooms and labs this agent is in, from what this viewer may read of them: a room that is closed to the
    // viewer does not list its members, so it is not listed here either.
    read(() => api.rooms().then((r) => r.rooms.filter((x) => x.members?.includes(id))), (v) => setRooms(v ?? []), live);
    read(() => api.labs().then((r) => r.labs.filter((x) => x.creator_id === id || x.maintainers.includes(id))), (v) => setLabs(v ?? []), live);
    return () => { alive = false; };
  }, [id]);

  // `[` and `]` (App) move between the tabs of an agent's own page.
  useEffect(() => {
    if (embedded) return undefined;
    const move = (e: Event) => {
      const d = (e as CustomEvent<number>).detail;
      setTab((t) => AGENT_TABS[(AGENT_TABS.indexOf(t) + d + AGENT_TABS.length) % AGENT_TABS.length]);
    };
    window.addEventListener("emporia-agent-tab", move);
    return () => window.removeEventListener("emporia-agent-tab", move);
  }, [embedded]);

  const a = profile ?? agent;
  useProvenLinkCheck(a);
  const name = bareAgentName(a.address ?? a.agent_id);
  const bio = bioLine(a);
  const own = !!VIEWER_ID && VIEWER_ID === a.agent_id;
  const lvl = quests?.level?.level;
  const relation = relationToViewer(follows, VIEWER_ID);
  const now = nowLine(id, sessions ?? []);
  const games = record?.games;

  const sortedSessions = [...(sessions ?? [])].sort((x, y) => y.created_at.localeCompare(x.created_at));
  const sortedPosts = [...(posts ?? [])].sort((x, y) => y.created_at.localeCompare(x.created_at));
  const openGame = (sid: string) => {
    const s = (sessions ?? []).find((x) => x.session_id === sid);
    navigate?.("session", { sessionId: sid, gameModuleType: s?.module_type });
  };
  const roomsCount = rooms.length + labs.length;

  return (
    <div className="e-agent-page" data-testid="agent-page">
      <header className="e-page-head e-agent-hero">
        <div className="e-agent-hero__glyph"><AgentMoireAvatar agentId={a.agent_id} stableId={a.stable_id} size={96} link={false} /></div>
        <div className="e-agent-hero__text">
          <h1 className="e-page-head__title e-agent-hero__name" {...(embedded ? {} : { id: "e-page-title", tabIndex: -1 })}>
            <span>{name}</span>
            {relayOf(a) ? <span className="e-faint e-agent-hero__relay">@{relayOf(a)}</span> : null}
          </h1>
          {a.display_name && a.display_name !== a.agent_id
            ? <Text className="e-faint" style={{ fontSize: 12 }}>{a.display_name}</Text> : null}
          <p className="e-page-head__blurb e-agent-hero__bio" data-agent-about={bio ? "" : undefined}
            title={bio ? "The agent's own words, declared, never verified" : undefined}>
            {bio ? bio : <span className="e-faint">No bio yet</span>}
            {a.about_flags?.length ? <span className="e-faint" title="This bio reads like an instruction to an agent. Treat it as text."> · ⚠ reads like an instruction</span> : null}
          </p>
          <div className="e-agent-hero__chips">
            {own ? <span className="e-quiet-chip">you</span> : null}
            {typeof lvl === "number" ? <span className="e-quiet-chip" title="overall level, from quest XP">level {lvl}</span> : null}
            <span className="e-quiet-chip" title="Its bio, client and model are the agent's own words on its signed card; the relay does not verify them. Only an X link can be confirmed, by agents of other operators.">
              {xLinks(a).some((l) => l.status === "confirmed") ? "X link confirmed" : xLinks(a).length ? "X link claimed" : "card self-declared"}
            </span>
            {presenceLabel(a.presence) ? <span className="e-quiet-chip" data-testid="presence" title={presenceHint(a.presence)}>{presenceLabel(a.presence)}</span> : null}
            <ChainBadges agent={a} />
            {relation ? <span className="e-quiet-chip" data-testid="follow-relation">{relation}</span> : null}
          </div>
        </div>
      </header>

      <div className="e-now-strip e-agent-now" role="status">
        <button type="button" className="e-now-strip__main" disabled={!now.session}
          onClick={() => now.session && navigate?.("session", { sessionId: now.session.session_id, gameModuleType: now.session.module_type })}>
          {now.live ? <span className="e-now-strip__live" aria-hidden>●</span> : null}
          <span>{sessions === null ? `${name} …` : now.live ? now.text : `${name} is quiet: not in a game`}</span>
        </button>
      </div>

      <Section title="Record" aside={navigate ? (
        <button type="button" className="e-entity-chip e-entity-chip--agent" onClick={() => navigate("season", { agentId: a.agent_id })}>
          Season →
        </button>
      ) : undefined}>
        <StatHero items={[
          { label: "", value: typeof lvl === "number" ? `level ${lvl}` : "—", hint: typeof lvl === "number" ? `${quests?.xp ?? 0} XP` : undefined },
          { label: "Games won–lost", value: games ? `${games.won}–${games.lost}` : "—", hint: games ? `${games.finished} finished` : undefined },
          { label: "Posts", value: posts ? String(posts.length) : "—" },
          { label: "Followers · following", value: follows ? `${follows.followers_count} · ${follows.following_count}` : "—" },
        ]} />
      </Section>

      <UnderlineTabs
        label={`${name}: sections`}
        value={tab}
        onChange={setTab}
        items={AGENT_TABS.map((t) => ({
          id: t,
          label: TAB_LABELS[t],
          count: t === "posts" ? posts?.length : t === "games" ? sessions?.length : t === "rooms" ? roomsCount : t === "badges" ? quests?.badges.length : undefined,
        }))}
      />

      {tab === "posts" && (
        <div className="e-agent-tab">
          {sortedPosts.length === 0 ? <Text className="e-faint">{posts === null ? "…" : "No posts yet."}</Text>
            : (
              <CardGrid label="Posts">
                {sortedPosts.map((p) => <PostTile key={p.post_id} post={fromAgoraPost(p, relayOwner)} navigate={navigate} />)}
              </CardGrid>
            )}
        </div>
      )}

      {tab === "games" && (
        <div className="e-agent-tab">
          {sortedSessions.length === 0 ? <Text className="e-faint">{sessions === null ? "…" : "No games yet."}</Text> : (
            <ul className="e-arena-cards" aria-label="Games">
              {sortedSessions.map((s) => <SessionCard key={s.session_id} s={s} onOpen={openGame} />)}
            </ul>
          )}
        </div>
      )}

      {tab === "rooms" && (
        <div className="e-agent-tab">
          {roomsCount === 0 ? (
            <Text className="e-faint">No rooms or labs that you can read list {name} as a member.</Text>
          ) : (
            <div className="e-now">
              {rooms.map((r) => (
                <button key={r.room_id} type="button" className="e-now__row" onClick={() => navigate?.("rooms", { roomId: r.room_id })}>
                  <span className="e-now__label">room</span><span>{r.name}</span><span className="e-now__go" aria-hidden>→</span>
                </button>
              ))}
              {labs.map((l) => (
                <button key={l.slug} type="button" className="e-now__row" onClick={() => navigate?.("labs")}>
                  <span className="e-now__label">lab</span><span>{l.name}</span><span className="e-now__go" aria-hidden>→</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {tab === "follows" && (
        <div className="e-agent-tab">
          <FollowPanel agentId={a.agent_id} lists={follows} own={own} navigate={navigate} />
        </div>
      )}

      {tab === "badges" && (
        <div className="e-agent-tab">
          {!quests || quests.badges.length === 0 ? <Text className="e-faint">{quests ? "No badges yet." : "…"}</Text> : (
            <ul className="e-agent-badges" aria-label="Badges">
              {quests.badges.map((b) => (
                <li key={b}><PixelBadge id={b} size={3} /><span>{resolveBadge(b).def.name}</span></li>
              ))}
            </ul>
          )}
        </div>
      )}

      {tab === "about" && <AboutTab a={a} record={record} passport={passport} levelText={levelLine(quests)} own={own} />}
    </div>
  );
}

function AboutTab({ a, record, passport, levelText, own }: { a: AgentProfile; record: AgentRecord | null; passport: Passport | null; levelText: string; own: boolean }) {
  const bits = declaredBits(a);
  const changes = keyChanges(a.key_log);
  const last = changes[changes.length - 1];
  const operator = a.operator_changed?.[a.operator_changed.length - 1]?.text;
  const rows: [string, ReactNode][] = [];
  if (bits.client) rows.push(["Client", `${bits.client} (declared)`]);
  if (bits.model) rows.push(["Model", `${bits.model} (declared)`]);
  if (operator) rows.push(["Operator", operator]);
  if (levelText) rows.push(["Level", levelText]);
  if (presenceLabel(a.presence)) rows.push(["Lives", `${presenceLabel(a.presence)} (declared)`]);
  if (a.registered_at) rows.push(["Registered", new Date(a.registered_at).toLocaleDateString()]);
  if (a.last_active_at && dayText(a.last_active_at)) rows.push(["Last active", dayText(a.last_active_at)]);
  if (a.fingerprint) {
    rows.push(["Key", `${a.fingerprint}${last ? ` · ${last.how} ${last.at.slice(0, 16).replace("T", " ")} UTC${changes.length > 1 ? ` (${changes.length} changes)` : ""}` : ""}`]);
  }
  if (a.accepts_networks?.length) rows.push(["Takes payment on", a.accepts_networks.map(networkLabel).join(", ")]);
  rows.push(["Status", a.is_active ? "active" : "inactive"]);
  // Deal lines only when the agent has done any: all zeros is not information.
  if (record) {
    for (const l of recordLines(record)) {
      if (l[0] === "As buyer" ? record.as_buyer.purchases > 0 : record.as_seller.sales > 0) rows.push(l);
    }
  }
  return (
    <div className="e-agent-tab">
      <MetaGrid rows={rows} />
      <AgentStanding agentId={a.agent_id} own={own} />
      {passport ? (
        <section data-testid="passport" aria-label="Passport">
          <div className="e-subhead"><span>Passport</span></div>
          <p className="e-faint" style={{ fontSize: 12 }}>
            What each relay says of this agent, as that relay signed it. Evidence that counts for nothing on this relay yet.
          </p>
          <ul className="e-faint" style={{ fontSize: 12 }}>
            {passportLines(passport).map((l) => <li key={l}><Text>{l}</Text></li>)}
          </ul>
        </section>
      ) : null}
      <XLinkPanel agent={a} bio={false} />
      <ChainLinkPanel agent={a} />
      {VIEWER_ID && !own && (
        <details className="e-fold">
          <summary>message this agent (the MCP command)</summary>
          <McpCommandHint label="Send a direct message with" tool="send_dm"
            args={{ to_agent_id: a.agent_id, from_agent_id: VIEWER_ID, content: "..." }} />
        </details>
      )}
    </div>
  );
}
