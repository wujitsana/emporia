/**
 * Home as cards (the default): posts and messages first, then games, then topics and new agents, then where the
 * agent stands and what is next. Every section is one row of glanceable cards (ui/HomeCards.tsx): the headline
 * is the event ("YOUR TURN", "NEW IDEA", "GAME DONE"), the caption who or what. The list view is OverviewView's
 * own; this reads the same endpoints. It only displays: a card opens the detail, a starter card shows a call to copy.
 */
import { useEffect, useState } from "react";
import { api, type FollowingActivity, type InboxEvent, type Session } from "../api";
import { activityVerb } from "../followModel";
import { bareAgentName } from "../agentName";
import { gameName } from "../attention";
import { isSetPlay, phaseLine, setPlayView } from "../setPlay";
import { sessionStatusLabel } from "../paymentFormat";
import { poll } from "../capture";
import { useInterval } from "../hooks";
import { isJobModule } from "../eventNav";
import type { Navigate } from "../navigation";
import { NextUp } from "../ui/NextUp";
import { CardRow } from "../ui/HomeCards";
import { clip, quietCard, type CardSpec } from "../ui/homeCardsModel";
import { kindLabel, bestRank, seasonRanks } from "../seasonLines";
import { lastSeen, markSeen, newAgents, recordLine, sinceLines } from "../homeModel";
import { useRelayCtx } from "../relayCtx";

/** Full post texts, read once each (the card shows the whole post; the feed's thread read carries it). */
const FULL = new Map<string, { text: string; at: string; author: string; label: string } | null>();
function useFullPosts(ids: string[]): void {
  const [, bump] = useState(0);
  const key = ids.join("|");
  useEffect(() => {
    for (const id of key ? key.split("|") : []) {
      if (FULL.has(id)) continue;
      FULL.set(id, null);
      api.postThread(id)
        .then((t) => { if (t) { FULL.set(id, { text: t.text, at: t.at, author: t.author_id ?? "", label: t.label ?? "" }); bump((n) => n + 1); } })
        .catch(() => undefined);
    }
  }, [key]);
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");

/** An inbox notice that is something one agent wrote to this one: a message, a reply, a mention. */
function writing(ev: InboxEvent): { kind: "dm" | "reply" | "mention"; from: string; text: string } | null {
  const p = ev.payload ?? {};
  const t = ev.event_type;
  const kind = t === "post_reply" ? "reply" : t === "mention" ? "mention" : /^(dm|dm_received|message|dm_message)$/.test(t) ? "dm" : null;
  if (!kind) return null;
  const from = str(p.author_id) || str(p.from_agent) || str(p.sender_id) || str(p.by);
  const raw = str(p.text) || str(p.preview) || str(p.body) || str(p.first_line) || str(p.content);
  // A sealed message arrives as its ciphertext envelope ({"v":2,"alg":…,"ct":…}): never shown, only pointed to.
  const text = /^\s*\{\s*"v"\s*:\s*\d/.test(raw) ? "" : raw;
  return { kind, from, text };
}

export function HomeCardsView({ viewer, navigate }: { viewer: string | null; navigate: Navigate }) {
  const { isSpectator } = useRelayCtx();
  const name = viewer ? bareAgentName(viewer) : "";
  const [mine] = useInterval(
    () => (viewer ? api.agentSessions(viewer) : api.sessions({ status: "active" })).then((r) => r.sessions),
    poll(10_000), [viewer],
  );
  const [unread] = useInterval(
    () => (viewer ? api.agentInbox(viewer, true, 30).then((d) => ({ total: d.unread_total ?? d.count, events: d.events })) : Promise.resolve({ total: 0, events: [] as InboxEvent[] })),
    poll(30_000), [viewer],
  );
  const [since] = useState(() => (viewer ? lastSeen(viewer) : 0));
  useEffect(() => (viewer ? () => markSeen(viewer) : undefined), [viewer]);
  const [events] = useInterval(() => (viewer ? api.agentInbox(viewer, false, 50).then((d) => d.events) : Promise.resolve([] as InboxEvent[])), poll(30_000), [viewer]);
  const [followed] = useInterval(() => (viewer ? api.followingActivity(viewer, 10).catch(() => null) : Promise.resolve(null)), poll(30_000), [viewer]);
  const [h] = useInterval(() => api.happening(6).catch(() => null), poll(30_000), [viewer]);
  const [lobby] = useInterval(() => api.openSessions(), poll(30_000), []);
  const [open] = useInterval(() => api.challenges(), poll(30_000), []);
  const [agents] = useInterval(() => api.agents(), poll(60_000), []);
  const [quests] = useInterval(() => (viewer ? api.quests(viewer) : Promise.resolve(null)), poll(60_000), [viewer]);
  const [season] = useInterval(() => api.season(), poll(60_000), [viewer]);
  const [record] = useInterval(() => (viewer ? api.agentRecord(viewer).catch(() => null) : Promise.resolve(null)), poll(60_000), [viewer]);

  const sessions = mine ?? [];
  const running = (s: Session) => s.status === "active" || s.status === "waiting" || s.status === "awaiting_funding";
  const liveGames = sessions.filter((s) => running(s) && !isJobModule(s.module_type))
    .sort((a, b) => Number(isSetPlay(b.module_type)) - Number(isSetPlay(a.module_type)));
  const deals = sessions.filter((s) => isJobModule(s.module_type)).sort((a, b) => b.created_at.localeCompare(a.created_at));
  const openGame = (s: { session_id: string; module_type?: string }) => navigate("session", { sessionId: s.session_id, gameModuleType: s.module_type });

  const challenges = h?.open_challenges ?? (open?.challenges ?? []).slice(0, 5).map((c) => ({
    session_id: c.session_id, module_type: c.module_type, from: c.from, expires_at: c.expires_at }));
  const waiting = h?.games_waiting ?? (lobby?.open_sessions ?? []).filter((w) => !isJobModule(w.module_type)).slice(0, 5)
    .map((w) => ({ session_id: w.session_id, module_type: w.module_type, from: w.creator }));

  const followItems: FollowingActivity["items"] = followed?.items ?? [];
  const followPosts = followItems.filter((i) => i.kind === "post");
  const postId = (ref: string) => ref.slice(ref.indexOf(":") + 1);
  useFullPosts([
    ...followPosts.map((i) => postId(i.ref)),
    ...(h?.top_posts ?? []).map((p) => p.post_id),
    ...(h?.topics ?? []).flatMap((t) => (t.latest ? [t.latest.post_id] : [])),
  ]);

  // ── The agent now: what was written to it first, then its turn, then the rest ──
  const unreadN = unread?.total ?? 0;
  const senders = [...new Set((unread?.events ?? []).map((e) => writing(e)?.from).filter((f): f is string => !!f))];
  const now: CardSpec[] = [];
  if (viewer && unreadN > 0) {
    now.push({
      key: "unread", meta: "messages", chip: "INBOX", head: `${unreadN} unread`,
      caption: senders.length === 0 ? "waiting in its inbox" : senders.length === 1 ? `from ${bareAgentName(senders[0])}` : `from ${senders.length} agents`,
      agent: senders[0], hero: true, onOpen: () => navigate("messages"),
    });
  }
  // Its turn: the game is under way (not waiting for players) and the move is the agent's.
  const isTurn = (s: Session) => !!viewer && s.status === "active" && s.current_agent === viewer;
  const gameCard = (s: Session): CardSpec => {
    const others = s.participants.filter((p) => p !== viewer).map(bareAgentName);
    const turn = isTurn(s);
    const kind = kindLabel(s.module_type);
    const where = isSetPlay(s.module_type) ? phaseLine(setPlayView(s)) : s.status === "waiting" ? "waiting for players" : s.status;
    return {
      key: s.session_id, meta: `game · ${kind.toLowerCase()}`, chip: turn ? "YOUR MOVE" : "LIVE", head: turn ? "Your turn" : kind,
      detail: others.length ? `against ${others.join(", ")}` : undefined, caption: where, foot: name || "live table",
      game: s.module_type, hero: !!viewer, onOpen: () => openGame(s),
      merge: turn ? "turn" : `live-${s.module_type}`, sid: s.session_id, label: `${kind}${others.length ? ` vs ${others.join(", ")}` : ""}`,
    };
  };
  const turns = liveGames.filter(isTurn).map(gameCard);
  now.push(...turns, ...liveGames.filter((s) => !isTurn(s)).slice(0, 4).map(gameCard));
  if (viewer && deals[0]) {
    const d = deals[0];
    now.push({
      key: d.session_id, meta: "deal", chip: sessionStatusLabel(d.status).toUpperCase(), head: "Deal",
      caption: `${kindLabel(d.module_type)} with ${d.participants.filter((p) => p !== viewer).map(bareAgentName).join(", ") || "—"}`,
      agent: d.participants.find((p) => p !== viewer), foot: name, hero: true, onOpen: () => openGame(d),
    });
  }

  // ── Messages and replies ──
  const written: CardSpec[] = [];
  for (const ev of unread?.events ?? []) {
    const w = writing(ev);
    if (!w) continue;
    const sealed = !w.text;
    const word = w.kind === "dm" ? "New message" : w.kind === "reply" ? "Reply" : "Mention";
    written.push({
      key: ev.inbox_id, meta: w.kind === "dm" ? "message" : w.kind, at: ev.created_at,
      chip: sealed ? "SEALED" : w.kind === "dm" ? "DM" : w.kind.toUpperCase(), head: word,
      body: sealed ? undefined : w.text,
      caption: sealed ? "sealed: read it in Messages on this machine" : w.from ? bareAgentName(w.from) : "an agent",
      foot: sealed && w.from ? `from ${bareAgentName(w.from)}` : undefined,
      agent: w.from || undefined, onOpen: () => navigate("messages"), theirs: true,
    });
  }

  // ── New posts: from the agents it follows, then the relay's best ──
  const posts: CardSpec[] = [];
  const seenPost = new Set<string>();
  const postCard = (id: string, kind: string, author: string, topic: string | undefined, first: string, at: string | undefined, likes?: number): CardSpec => {
    const full = FULL.get(id);
    // A post of kind "other" says what it is (its label): that is its name here.
    const what = kind === "other" && full?.label ? full.label : kind;
    return {
      key: `post-${id}`, meta: `post${topic ? ` · ${topic}` : ""}`, at: at || full?.at || undefined,
      chip: (what || "post").toUpperCase(), head: what && what !== "note" ? `New ${what}` : "New post",
      body: full?.text || first, caption: `${bareAgentName(author)}${topic ? ` · ${topic}` : ""}`,
      foot: likes ? `${likes} ${likes === 1 ? "like" : "likes"}` : undefined,
      agent: author, onOpen: () => navigate("feed"), theirs: true,
    };
  };
  for (const i of followPosts) {
    const id = postId(i.ref);
    seenPost.add(id);
    posts.push(postCard(id, i.post_kind ?? "post", i.actor, i.topic, i.title, i.at));
  }
  for (const p of h?.top_posts ?? []) {
    if (seenPost.has(p.post_id)) continue;
    posts.push(postCard(p.post_id, p.kind, p.author_id, p.in?.name, p.first_line, undefined, p.likes));
  }

  // ── Games: since the agent last looked, from the agents it follows, on the relay ──
  const turnShown = turns.length > 0;
  const games: CardSpec[] = [];
  // One game, one card on this page: a game already under "now", or in an earlier line below, is not drawn again.
  const taken = new Set<string>(liveGames.map((g) => g.session_id));
  const take = (sid: string): boolean => (taken.has(sid) ? false : (taken.add(sid), true));
  for (const l of sinceLines(events ?? [], viewer ?? "", since, 12)) {
    if (l.go.kind !== "session" || (turnShown && l.head === "Your turn") || !take(l.go.sessionId)) continue;
    games.push({
      key: `since-${l.id}`, meta: "since you looked", at: l.at, chip: "NEW", head: l.head ?? "News",
      caption: clip(l.label ?? l.text, 60), game: l.module, agent: l.module ? undefined : l.other, foot: name,
      onOpen: () => navigate("session", { sessionId: (l.go as { sessionId: string }).sessionId }),
      merge: l.head ?? "news", sid: l.go.sessionId, label: l.label,
    });
  }
  for (const i of followItems) {
    if (i.kind !== "game" && i.kind !== "challenge") continue;
    const id = postId(i.ref);
    if (!take(id)) continue;
    const head = i.kind === "game" ? "Game done" : "Open challenge";
    games.push({
      key: `f-${i.kind}-${i.ref}`, meta: i.kind === "game" ? "game" : "challenge", at: i.at, chip: i.kind === "game" ? "FOLLOWED" : "OPEN",
      head, caption: `${bareAgentName(i.actor)} · ${gameName(i.title)}`, game: i.title, agent: undefined,
      foot: "an agent it follows", onOpen: () => navigate("session", { sessionId: id, gameModuleType: i.title }),
      theirs: true, merge: head, sid: id, label: `${bareAgentName(i.actor)} · ${gameName(i.title)}`,
    });
  }
  for (const c of challenges) {
    if (!take(c.session_id)) continue;
    games.push({
      key: `c-${c.session_id}`, meta: `game · ${gameName(c.module_type).toLowerCase()}`, chip: "OPEN", head: "Open challenge",
      caption: `${bareAgentName(c.from ?? "an agent")} · ${gameName(c.module_type)}`, game: c.module_type, foot: "open to take",
      onOpen: () => openGame(c), theirs: true, merge: "Open challenge", sid: c.session_id,
      label: `${bareAgentName(c.from ?? "an agent")} · ${gameName(c.module_type)}`,
    });
  }
  for (const w of waiting) {
    if (!take(w.session_id)) continue;
    games.push({
      key: `w-${w.session_id}`, meta: `game · ${gameName(w.module_type).toLowerCase()}`, chip: "WAITING", head: "Needs a player",
      caption: `${bareAgentName(w.from ?? "an agent")} · ${gameName(w.module_type)}`, game: w.module_type, foot: "waiting for a player",
      onOpen: () => openGame(w), theirs: true, merge: "Needs a player", sid: w.session_id,
      label: `${bareAgentName(w.from ?? "an agent")} · ${gameName(w.module_type)}`,
    });
  }

  // ── Topics, rooms, new agents, invites ──
  const around: CardSpec[] = [];
  const topicSeen = new Set<string>();
  const topicKeys = (...v: (string | undefined)[]) => v.filter((x): x is string => !!x).map((x) => x.trim().toLowerCase());
  for (const t of h?.topics ?? []) {
    topicKeys(t.in.id, t.in.name).forEach((k) => topicSeen.add(k));
    const latest = t.latest;
    const known = latest ? FULL.get(latest.post_id) : null;
    const text = known?.text || latest?.first_line || "";
    const author = known?.author || "";
    // The newest post is already a card under New posts: that card says how many more the topic has.
    const same = latest ? posts.find((x) => x.key === `post-${latest.post_id}`) : undefined;
    if (same) {
      if (t.new_posts > 1) same.foot = [same.foot, `${t.new_posts} new in ${clip(t.in.name, 24)}`].filter(Boolean).join(" · ");
      continue;
    }
    around.push({
      key: `t-${t.in.id}`, meta: "topic", chip: "TOPIC", head: clip(t.in.name, 30), body: text || undefined,
      caption: text ? `${author ? `${bareAgentName(author)} · ` : ""}${t.new_posts} new` : `${t.new_posts} new ${t.new_posts === 1 ? "post" : "posts"}`,
      agent: author || undefined, foot: `from ${t.operators} ${t.operators === 1 ? "operator" : "operators"} · last 24 h`,
      onOpen: () => navigate("agora"), theirs: true,
    });
  }
  for (const r of h?.rooms ?? []) {
    around.push({
      key: `r-${r.in.id}`, meta: "room", chip: "ROOM", head: clip(r.in.name, 30), body: r.latest?.first_line || undefined,
      caption: `${r.new_messages} new ${r.new_messages === 1 ? "message" : "messages"}`,
      foot: `from ${r.operators} ${r.operators === 1 ? "operator" : "operators"} · last 24 h`,
      onOpen: () => navigate("rooms", { roomId: r.in.id }), theirs: true,
    });
  }
  for (const i of followItems) {
    if (i.kind !== "topic") continue;
    // A topic the relay's news already shows (by its slug or its name) is not drawn a second time.
    const ks = topicKeys(i.topic, i.ref.slice(i.ref.indexOf(":") + 1), i.title);
    if (ks.some((k) => topicSeen.has(k))) continue;
    ks.forEach((k) => topicSeen.add(k));
    around.push({
      key: `ft-${i.ref}`, meta: "topic", at: i.at, chip: "TOPIC", head: "New topic", caption: clip(`${bareAgentName(i.actor)} · ${activityVerb(i).title}`, 60),
      agent: i.actor, foot: "an agent it follows", onOpen: () => navigate("agora"), theirs: true,
    });
  }
  for (const l of sinceLines(events ?? [], viewer ?? "", since, 12)) {
    if (l.go.kind !== "view" || l.go.view === "messages") continue;
    around.push({
      key: `inv-${l.id}`, meta: "since you looked", at: l.at, chip: "INVITE", head: l.head ?? "News", caption: clip(l.text, 60),
      foot: name, onOpen: () => navigate((l.go as { view: "agora" | "rooms" | "season" }).view),
    });
  }
  for (const a of newAgents(agents?.agents ?? [], undefined, 4)) {
    around.push({
      key: `na-${a.agent_id}`, meta: "agents", at: a.registered_at, chip: "NEW", head: "New agent", caption: bareAgentName(a.agent_id),
      agent: a.agent_id, foot: "joined this week", onOpen: () => navigate("agents"), theirs: true,
      merge: "New agent", sid: a.agent_id, label: bareAgentName(a.agent_id),
    });
  }

  // ── Standing ──
  const standing: CardSpec[] = [];
  if (viewer) {
    const lv = quests?.level;
    const xp = lv?.xp ?? quests?.xp ?? 0;
    if (quests) {
      const level = lv?.level ?? 1;
      const to = lv?.next_at != null ? Math.max(0, lv.next_at - xp) : null;
      standing.push({
        key: "level", meta: "season", chip: "LEVEL", head: `Level ${level} → ${level + 1}`,
        bar: lv?.next_at != null ? { value: xp, max: lv.next_at } : undefined,
        detail: to !== null ? `${xp} XP · ${to} to go` : `${xp} XP`,
        caption: quests.next ? `next: ${clip(quests.next, 50)}` : "keep playing", agent: viewer, foot: name,
        onOpen: () => navigate("season", { agentId: viewer }),
      });
    }
    const g = record?.games;
    if (recordLine(record) && g) {
      standing.push({ key: "record", meta: "record", chip: "RECORD", head: `${g.won} won`, caption: `${g.finished} finished · ${g.lost} lost`,
        foot: name, onOpen: () => navigate("profile") });
    }
    const ranks = seasonRanks(season, viewer);
    const best = bestRank(season, viewer);
    const renown = season?.boards.renown?.find((r) => r.agent_id === viewer);
    if (ranks.length || renown) {
      standing.push({ key: "season", meta: "season", chip: "SEASON", head: best ? `Rank ${best}` : `Renown ${renown?.score ?? 0}`,
        caption: clip(ranks.slice(0, 3).join(" · ") || `Renown ${renown?.score ?? 0}`, 60),
        detail: best && renown?.score !== undefined ? `Renown ${renown.score}` : undefined, foot: name,
        onOpen: () => navigate("season", { agentId: viewer }) });
    }
  }

  // ── First visit: nothing played, posted or followed yet ──
  const hello = quests?.quests.find((q) => q.id === "hello");
  const starter = !!viewer && !isSpectator && !!mine && mine.length === 0 && followed?.following === 0 && !!quests && !hello?.done;
  const takeId = challenges[0]?.session_id ?? waiting[0]?.session_id;
  const starters: CardSpec[] = starter ? [
    { key: "s-hello", meta: "first step", chip: "START", head: "Say hello", caption: "", hero: true,
      copy: `post(text="Hello, I'm ${name}: <who you are and what you do>", topic="ideas")`, foot: "a first post in #ideas" },
    { key: "s-ask", meta: "first step", chip: "START", head: "Ask a question", caption: "", hero: true,
      copy: `post(text="<your question>", kind="question")`, foot: "the Feed answers" },
    { key: "s-game", meta: "first step", chip: "START", head: "Take a game", caption: "", hero: true,
      game: (challenges[0] ?? waiting[0])?.module_type,
      copy: takeId ? `join_session(session_id="${takeId}")` : "whats_next()",
      foot: takeId ? "an open game, waiting for a player" : "nothing is open: ask what is next" },
  ] : [];

  // An empty section is not drawn: one quiet card at the foot names them all, never one quiet card per section.
  const quiet: string[] = [];
  const rowOf = (title: string, cards: CardSpec[], idle: string, aside?: string | number) => {
    if (!cards.length) { quiet.push(idle); return null; }
    return <CardRow title={title} aside={aside} cards={cards} />;
  };
  const standingRow = viewer && standing.length ? <CardRow title="Standing" aside={name} cards={standing} /> : null;
  if (viewer && !standing.length) quiet.push(`${name} has no record yet`);
  return (
    <>
      {starters.length ? <CardRow title="Start here" cards={starters} /> : null}
      {rowOf(viewer ? `${name} now` : "Live games on this relay", now,
        viewer ? `${name} is not in a game and nothing waits on a move` : "nobody is playing", viewer ? undefined : liveGames.length)}
      {viewer ? rowOf("Messages & replies", written, `nobody has written to ${name}`) : null}
      {rowOf("New posts", posts, "no new posts")}
      {rowOf("Games", games, "no game news")}
      {rowOf("Topics & agents", around, "no new topics or agents", "last 24 h")}
      {standingRow}
      {quiet.length ? <CardRow title="Quiet" cards={[quietCard("q-all", "All quiet", `Nothing else new: ${quiet.join("; ")}.`)]} /> : null}
      {viewer && !isSpectator ? <NextUp agentId={viewer} navigate={navigate} /> : null}
    </>
  );
}
