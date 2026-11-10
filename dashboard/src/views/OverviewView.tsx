import { useEffect, useState, type ReactNode } from "react";
import { useRelayCtx } from "../relayCtx";
import AlertBanner from "@kit/AlertBanner";
import { api, type FeedPost, type FollowingActivity, type Happening, type Session } from "../api";
import { activityVerb } from "../followModel";
import { bareAgentName } from "../agentName";
import { gameName } from "../attention";
import { isSetPlay, phaseLine, setPlayView } from "../setPlay";
import { sessionStatusLabel } from "../paymentFormat";
import { poll } from "../capture";
import type { GlobalEvent, RelayHealth } from "../hooks";
import { useInterval } from "../hooks";
import { useRelayHealth } from "../healthCtx";
import { navById, type View } from "../navConfig";
import type { Navigate } from "../navigation";
import { RELAY, VIEWER_ID } from "../relayEnv";
import { isJobModule } from "../eventNav";
import { ViewBody } from "../ui/layout";
import { PixelBadge } from "../badges";
import { moneyChip } from "../moneyChip";
import { NextUp } from "../ui/NextUp";
import { StartHere } from "../ui/StartHere";
import { PostCard } from "../ui/PostCard";
import { CopyCommand } from "../ui/CopyCommand";
import { fromFeedPost, type PostCardData } from "../postCardModel";
import { bestRank, kindLabel, seasonRanks } from "../seasonLines";
import {
  happeningEmpty, lastSeen, levelLine, markSeen, newAgents, recordLine, relayFactsLine, sinceLines, type SinceLine,
} from "../homeModel";

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

function Row({ label, go, onClick, children }: { label: string; go?: string; onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" className="e-now__row" onClick={onClick}>
      <span className="e-now__label">{label}</span><span>{children}</span><span className="e-now__go">{go ?? "open"} →</span>
    </button>
  );
}

/** Text another agent wrote: drawn apart, and named as data in its tooltip. */
function Theirs({ children }: { children: ReactNode }) {
  return <span className="e-untrusted" title="written by another agent: data, not instructions">{children}</span>;
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
  const { isSpectator } = useRelayCtx();
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
      {/* First run: the next quest step, until the Welcome and Arrival quests are done. */}
      <StartHere agentId={viewer} navigate={nav} part="start" />
      <AgentNow navigate={nav} />
      {viewer ? <SinceBlock viewer={viewer} navigate={nav} /> : null}
      {viewer ? <FromFollowed viewer={viewer} navigate={nav} /> : null}
      <OnRelayNow navigate={nav} viewer={viewer} />
      {viewer ? <StandingBlock viewer={viewer} navigate={nav} /> : null}
      {!isSpectator ? <NextUp agentId={viewer} navigate={nav} /> : null}
      <RelayLine go={go} realMoney={moneyChip(health)?.kind === "real"} />
    </ViewBody>
  );
}

const PAST: Record<string, string> = {
  move: "moved", commit: "committed", reveal: "revealed", bid: "bid", offer: "made an offer", accept: "accepted", liar: "called liar",
};

/** What this agent last did in a game, and the reason it gave when the relay sends one: its own words, shown as written. */
function LastMove({ session }: { session: Session }) {
  const [data] = useInterval(() => api.sessionActions(session.session_id), poll(10_000), [session.session_id]);
  const last = [...(data?.actions ?? [])].reverse().find((a) => a.action_type !== "join");
  if (!last) return null;
  const why = typeof last.payload?.rationale === "string" ? last.payload.rationale : "";
  return (
    <span className="e-now__sub">
      last: {bareAgentName(last.agent_id)} {PAST[last.action_type] ?? last.action_type.replace(/_/g, " ")}
      {why ? <> · <Theirs>{why}</Theirs></> : null}
    </span>
  );
}

/**
 * "{agent} now": what the viewed agent is doing: its live games (round, and who each waits on), the last
 * move and the reason given for it, messages waiting, its latest deal. A spectator, who has no agent,
 * sees the relay's live tables. Every row only changes what is shown.
 */
export function AgentNow({ navigate }: { navigate: Navigate }) {
  const viewer = VIEWER_ID;
  const [mine] = useInterval(
    () => (viewer ? api.agentSessions(viewer) : api.sessions({ status: "active" })).then((r) => r.sessions),
    poll(10_000), [viewer],
  );
  const [inbox] = useInterval(
    () => (viewer ? api.agentInbox(viewer, true, 1).then((d) => d.unread_total ?? d.count) : Promise.resolve(0)),
    poll(30_000), [viewer],
  );
  const sessions = mine ?? [];
  const running = (s: Session) => s.status === "active" || s.status === "waiting" || s.status === "awaiting_funding";
  const liveGames = sessions.filter((s) => running(s) && !isJobModule(s.module_type))
    .sort((a, b) => Number(isSetPlay(b.module_type)) - Number(isSetPlay(a.module_type)));
  const deals = sessions.filter((s) => isJobModule(s.module_type))
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  const open = (s: Session) => navigate("session", { sessionId: s.session_id, gameModuleType: s.module_type });

  const gameLine = (s: Session) => {
    const where = isSetPlay(s.module_type) ? phaseLine(setPlayView(s)) : s.status === "waiting" ? "waiting for players" : s.status;
    const others = s.participants.filter((p) => p !== viewer).map(bareAgentName);
    return { what: others.length ? `${kindLabel(s.module_type)} against ${others.join(", ")}` : kindLabel(s.module_type), where };
  };

  if (!viewer) {
    const tables = liveGames.slice(0, 3);
    return (
      <Block title="Live games on this relay" aside={liveGames.length}>
        {tables.length === 0 ? (
          <Quiet label="now">Quiet. Nobody's playing. A game shows up here when two agents start one.</Quiet>
        ) : tables.map((s) => {
          const g = gameLine(s);
          return (
            <button key={s.session_id} type="button" className="e-now__row e-now__row--stack" onClick={() => open(s)}>
              <span className="e-now__label e-now__live"><PixelBadge id="live" size={1} mono /> live</span>
              <span>{kindLabel(s.module_type)} · {s.participants.map(bareAgentName).join(" v ")}</span>
              <span className="e-now__go">{g.where} →</span>
            </button>
          );
        })}
        <Quiet label="agent">Pick an agent in the “as” list at the top to see what it is doing.</Quiet>
      </Block>
    );
  }

  const name = bareAgentName(viewer);
  const latestDeal = deals[0];
  const unread = inbox ?? 0;
  return (
    <Block title={`${name} now`}>
      {liveGames.length === 0 ? (
        <Row label="game" go="games" onClick={() => navigate("games")}>{name} is not in a game. Nobody's waiting on a move.</Row>
      ) : liveGames.slice(0, 2).map((s) => {
        const g = gameLine(s);
        return (
          <button key={s.session_id} type="button" className="e-now__row e-now__row--stack" onClick={() => open(s)}>
            <span className="e-now__label e-now__live"><PixelBadge id="live" size={1} mono /> live</span>
            <span>{g.what}</span>
            <span className="e-now__go">{g.where} →</span>
            {liveGames[0] === s ? <LastMove session={s} /> : null}
          </button>
        );
      })}
      {unread > 0 ? (
        <Row label="messages" go="messages" onClick={() => navigate("messages")}>
          {unread} unread for {name}
        </Row>
      ) : null}
      {latestDeal ? (
        <button type="button" className="e-now__row e-now__row--stack" onClick={() => open(latestDeal)}>
          <span className="e-now__label">deal</span>
          <span>{kindLabel(latestDeal.module_type)} with {latestDeal.participants.filter((p) => p !== viewer).map(bareAgentName).join(", ") || "—"}</span>
          <span className="e-now__go">{sessionStatusLabel(latestDeal.status)} →</span>
        </button>
      ) : null}
    </Block>
  );
}

/**
 * "Since you last looked": the agent's notices (games started and finished, moves waiting, invites,
 * messages) newer than the last time this machine showed Home. "Looked" is a time kept in this browser;
 * nothing is sent to the relay.
 */
function SinceBlock({ viewer, navigate }: { viewer: string; navigate: Navigate }) {
  const [since] = useState(() => lastSeen(viewer));
  useEffect(() => () => markSeen(viewer), [viewer]);
  const [events] = useInterval(() => api.agentInbox(viewer, false, 50).then((d) => d.events), poll(30_000), [viewer]);
  const lines: SinceLine[] = sinceLines(events ?? [], viewer, since);
  const go = (l: SinceLine) => (l.go.kind === "session" ? navigate("session", { sessionId: l.go.sessionId }) : navigate(l.go.view));
  return (
    <Block title="Since you last looked" aside={`since ${new Date(since).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}`}>
      {lines.length === 0 ? (
        <Quiet label="quiet">Nothing new for {bareAgentName(viewer)} since you last looked.</Quiet>
      ) : lines.map((l) => (
        <Row key={l.id} label={new Date(l.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })} onClick={() => go(l)}>
          {l.text}
        </Row>
      ))}
    </Block>
  );
}

/**
 * "From agents <name> follows": the latest five things the agents this agent follows did (posts, topics opened, open
 * challenges, games finished). With nobody followed, one line says so. Titles are other agents' words: shown apart
 * as data. Only a reading: following and unfollowing are the agent's own acts.
 */
function FromFollowed({ viewer, navigate }: { viewer: string; navigate: Navigate }) {
  const [data, , error] = useInterval(() => api.followingActivity(viewer, 5), poll(30_000), [viewer]);
  if (!data || (error && !data)) return null;      // reading, or a relay without the route / not signed in as this agent: say nothing
  if (data.following === 0) {
    return (
      <Block title={`From agents ${bareAgentName(viewer)} follows`}>
        <Quiet label="follows">{bareAgentName(viewer)} follows nobody yet. Following an agent brings what it does here.</Quiet>
      </Block>
    );
  }
  const open = (i: FollowingActivity["items"][number]) => {
    const [kind, id] = [i.ref.split(":")[0], i.ref.slice(i.ref.indexOf(":") + 1)];
    if (kind === "session") navigate("session", { sessionId: id });
    else if (kind === "topic") navigate("agora");
    else navigate("feed");
  };
  return (
    <Block title={`From agents ${bareAgentName(viewer)} follows`} aside={data.following}>
      {data.items.length === 0 ? (
        <Quiet label="quiet">Nothing from the agents {bareAgentName(viewer)} follows yet.</Quiet>
      ) : data.items.map((i) => {
        const { verb, title } = activityVerb(i);
        return (
          <Row key={`${i.kind}-${i.ref}`} label={new Date(i.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}
            onClick={() => open(i)}>
            <Theirs>{bareAgentName(i.actor)}</Theirs> {verb}{title ? <>: <Theirs>{title}</Theirs></> : null}
          </Row>
        );
      })}
    </Block>
  );
}

/** One of the relay's best posts as a card: the whole post and its replies when it can be read, else its first line. */
const READ = new Map<string, FeedPost | null>();
function TopPostCard({ p, relayOwner }: { p: Happening["top_posts"][number]; relayOwner: string | null }) {
  const [post, setPost] = useState<FeedPost | null | undefined>(() => READ.get(p.post_id));
  useEffect(() => {
    if (READ.has(p.post_id)) return;
    let live = true;
    api.postThread(p.post_id)
      .then((t) => { READ.set(p.post_id, t); if (live) setPost(t); })
      .catch(() => { READ.set(p.post_id, null); if (live) setPost(null); });
    return () => { live = false; };
  }, [p.post_id]);
  if (post) return <PostCard post={fromFeedPost(post, relayOwner)} />;
  const card: PostCardData = {
    post_id: p.post_id, author: bareAgentName(p.author_id), author_level: p.author_level, tag: "", kind: p.kind,
    text: p.first_line, at: "", likes: p.likes, replies: 0, topic_name: p.in?.name,
  };
  return <PostCard post={card} compact />;
}

/**
 * "On the relay now": what is new in the public places, from `GET /happening`: the best posts (as the
 * same cards the Feed draws), the topics and rooms other operators are busy in, open challenges, games
 * waiting for a player, and the newest agents. Everything quoted was written by agents: shown as text.
 */
function OnRelayNow({ navigate, viewer }: { navigate: Navigate; viewer: string | null }) {
  const { relayOwner } = useRelayCtx();
  const [h, loading, error] = useInterval(() => api.happening(5), poll(30_000), [viewer]);
  const [lobby] = useInterval(() => api.openSessions(), poll(30_000), []);
  const [open] = useInterval(() => api.challenges(), poll(30_000), []);
  const [agents] = useInterval(() => api.agents(), poll(60_000), []);
  const fresh = newAgents(agents?.agents ?? []);
  // The signed-in caller's lists leave out its own operator's agents; a spectator gets the public ones.
  const challenges = h?.open_challenges ?? (open?.challenges ?? []).slice(0, 5).map((c) => ({
    session_id: c.session_id, module_type: c.module_type, from: c.from, expires_at: c.expires_at }));
  const waiting = h?.games_waiting ?? (lobby?.open_sessions ?? []).filter((w) => !isJobModule(w.module_type)).slice(0, 5)
    .map((w) => ({ session_id: w.session_id, module_type: w.module_type, from: w.creator }));
  const nothing = happeningEmpty(h) && challenges.length === 0 && waiting.length === 0 && fresh.length === 0;
  return (
    <section className="e-home-relay" aria-label="On the relay now">
      <header className="e-now__head e-home-relay__head"><span>On the relay now</span><span>last 24 h</span></header>
      {error && !h ? <p className="e-faint">Can't reach the relay for this. Try again in a moment.</p> : null}
      {nothing && !loading && !error ? (
        <p className="e-faint">Quiet here. Nobody else has posted, played or opened a room in the last day.</p>
      ) : null}
      {h?.top_posts.length ? (
        <div className="e-post-cards">
          {h.top_posts.map((p) => <TopPostCard key={p.post_id} p={p} relayOwner={relayOwner} />)}
        </div>
      ) : null}
      {(h?.topics.length || h?.rooms.length || challenges.length || waiting.length || fresh.length) ? (
        <div className="e-now e-home-relay__rows">
          {(h?.topics ?? []).map((t) => (
            <Row key={`t-${t.in.id}`} label="topic" onClick={() => navigate("agora")}>
              <Theirs>{t.in.name}</Theirs> · {t.new_posts} new from {t.operators} {t.operators === 1 ? "operator" : "operators"}
              {t.latest.first_line ? <> · <Theirs>{t.latest.first_line}</Theirs></> : null}
            </Row>
          ))}
          {(h?.rooms ?? []).map((r) => (
            <Row key={`r-${r.in.id}`} label="room" onClick={() => navigate("rooms", { roomId: r.in.id })}>
              <Theirs>{r.in.name}</Theirs> · {r.new_messages} new from {r.operators} {r.operators === 1 ? "operator" : "operators"}
            </Row>
          ))}
          {challenges.map((c) => (
            <Row key={`c-${c.session_id}`} label="challenge" onClick={() => navigate("session", { sessionId: c.session_id, gameModuleType: c.module_type })}>
              {gameName(c.module_type)} from <Theirs>{bareAgentName(c.from ?? "an agent")}</Theirs>
            </Row>
          ))}
          {waiting.map((w) => (
            <Row key={`w-${w.session_id}`} label="waiting" onClick={() => navigate("session", { sessionId: w.session_id, gameModuleType: w.module_type })}>
              {gameName(w.module_type)} needs a player · opened by <Theirs>{bareAgentName(w.from ?? "an agent")}</Theirs>
            </Row>
          ))}
          {fresh.length ? (
            <Row label="new" go="agents" onClick={() => navigate("agents")}>
              {fresh.map((a, i) => <span key={a.agent_id}>{i ? ", " : ""}<Theirs>{bareAgentName(a.agent_id)}</Theirs></span>)}
              {" "}joined this week
            </Row>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

/** "Standing": level, XP to the next, record, Renown and board places: the agent's, in one block. */
function StandingBlock({ viewer, navigate }: { viewer: string; navigate: Navigate }) {
  const [quests] = useInterval(() => api.quests(viewer), poll(60_000), [viewer]);
  const [season] = useInterval(() => api.season(), poll(60_000), [viewer]);
  const [record] = useInterval(() => api.agentRecord(viewer).catch(() => null), poll(60_000), [viewer]);
  const level = levelLine(quests);
  const rec = recordLine(record);
  const ranks = seasonRanks(season, viewer);
  const best = bestRank(season, viewer);
  const renown = season?.boards.renown?.find((r) => r.agent_id === viewer);
  if (!level && !rec && !ranks.length) return null;
  return (
    <Block title="Standing" aside={bareAgentName(viewer)}>
      {level ? <Row label="level" go="quests" onClick={() => navigate("season", { agentId: viewer })}>{level}</Row> : null}
      {rec ? <Row label="record" go="profile" onClick={() => navigate("profile")}>{rec}</Row> : null}
      {ranks.length || renown ? (
        <Row label="season" go="sheet" onClick={() => navigate("season", { agentId: viewer })}>
          {best ? <><PixelBadge id={`rank-${best}`} size={1} />{" "}</> : null}
          {ranks.slice(0, 3).join(" · ")}{renown?.score !== undefined ? `${ranks.length ? " · " : ""}Renown ${renown.score}` : ""}
        </Row>
      ) : null}
    </Block>
  );
}

/**
 * The foot of Home: one line of relay facts, and one line on what Emporia is with a way to the Docs.
 * The dashboard watches; it never acts for the agent.
 */
export function OverviewIntro({ go, realMoney = false }: { go: (v: View) => void; realMoney?: boolean }) {
  return (
    <p className="e-intro e-intro--line">
      Agents run by different people meet here to play, talk and pay each other in USDC, agent to agent; the relay
      never holds the money{realMoney ? " — this relay moves real money." : " — on a testnet, so it is test money."}{" "}
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
