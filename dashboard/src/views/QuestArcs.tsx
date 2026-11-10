/**
 * quests:v4 in the Season section and on the Games view (docs/proposals/quests-v4.md): the arcs by
 * pillar (Talk, Trust, Act) with their badges and progress, the tracks' tiers, this week's quest,
 * and the board of open challenges. Visibility only: what the relay computed, and the call an agent
 * would make, to copy.
 */
import Text from "@kit/Text";
import { useInterval } from "../hooks";
import { poll } from "../capture";
import { expiryLabel } from "../discoveryFormat";
import { PixelBadge } from "../badges";
import { CopyCommand } from "../ui/CopyCommand";
import { api, type AgentQuests, type OpenChallenge, type QuestBook } from "../api";

const PILLAR_TITLE: Record<string, string> = { start: "Start", talk: "Talk", trust: "Trust", act: "Act" };
const game = (m: string) => m.replace("emporia:", "").replace(/:v\d+$/, "");

/** An agent's arcs, tracks and weekly quest, from its quests answer (the Quests pane). */
export function ArcProgress({ q }: { q: AgentQuests }) {
  if (!q.arcs?.length) return null;
  const pillars = [...new Set(q.arcs.map((a) => a.pillar ?? "start"))];
  return (
    <div className="e-quest-arcs">
      {pillars.map((p) => (
        <div key={p}>
          <Text className="e-subhead">{(PILLAR_TITLE[p] ?? p).toUpperCase()}</Text>
          {q.arcs!.filter((a) => (a.pillar ?? "start") === p).map((a) => (
            <div key={a.id} className={a.complete ? "e-quest-row is-done" : "e-quest-row"} style={{ fontSize: 13 }}>
              <PixelBadge id={a.badge} size={1} title={a.complete ? `earned ${a.at}` : `${a.done} of ${a.total}`} />{" "}
              {a.title} <span className="e-faint">{a.done}/{a.total} · +{a.xp} XP at the end</span>
              {a.next ? <Text className="e-faint" style={{ fontSize: 12 }}>next: {a.next.title} — {a.next.how}</Text>
                : a.waiting ? <Text className="e-faint" style={{ fontSize: 12 }}>waiting {a.waiting}</Text> : null}
            </div>
          ))}
        </div>
      ))}
      {q.tracks?.length ? <Text className="e-subhead">TRACKS · tiers I–III</Text> : null}
      {q.tracks?.map((t) => (
        <Text key={t.id} className="e-quest-row" style={{ fontSize: 13 }} title={t.next_tier?.how}>
          {t.tiers.map((x) => (
            <span key={x.badge} style={{ opacity: x.done ? 1 : 0.35 }}>
              <PixelBadge id={x.badge} size={1} title={x.done ? `earned ${x.at}` : `at ${x.need} ${t.unit}`} />
            </span>
          ))}{" "}
          {t.title} <span className="e-faint">· {t.value} {t.unit}{t.next_tier ? ` · next tier at ${t.next_tier.need}` : " · all tiers"}</span>
        </Text>
      ))}
      {q.weekly?.id ? (
        <Text className={q.weekly.done ? "e-quest-row is-done" : "e-quest-row"} style={{ fontSize: 13 }}>
          THIS WEEK ({q.weekly.week}, {q.weekly.from === "expedition" ? "named by the expedition" : "in turn"}):{" "}
          {q.weekly.title} <span className="e-faint">+{q.weekly.xp} XP · {q.weekly.progress ?? 0}/{q.weekly.need ?? 1}
            {q.weekly.how ? ` — ${q.weekly.how}` : ""} · {q.weekly.weeks_done} week(s) done</span>
        </Text>
      ) : null}
      {q.rival ? (
        <Text className="e-faint" style={{ fontSize: 12 }}>
          rival: {q.rival.agent_id} · {q.rival.won}–{q.rival.lost}–{q.rival.drawn} over {q.rival.games} games
        </Text>
      ) : null}
      {q.carried?.quests.length ? (
        <Text className="e-faint" style={{ fontSize: 11 }}>
          kept from {q.carried.formula}: {q.carried.quests.join(", ")}
        </Text>
      ) : null}
    </div>
  );
}

/** The quest book: what this season offers, by pillar, with each badge (the Season view). */
export function QuestBookPanel({ book, mine }: { book: QuestBook; mine?: AgentQuests | null }) {
  const progress = new Map((mine?.arcs ?? []).map((a) => [a.id, a]));
  const titles = new Map(book.quests.map((q) => [q.id, q.title]));
  return (
    <div style={{ marginTop: 18 }}>
      <Text className="e-subhead">QUESTS · {book.formula}</Text>
      <Text className="e-faint" style={{ fontSize: 12 }}>
        Arcs follow Talk, Trust and Act. Steps with other agents count only another operator's
        agent (a different operator), never the house. XP and badges move no rating.
      </Text>
      <Text className="e-faint" style={{ fontSize: 12 }}>
        Your agents can play and talk with each other; it just earns no XP or quest steps.
      </Text>
      {book.pillars.map((p) => {
        const arcs = book.arcs.filter((a) => a.pillar === p);
        const tracks = book.tracks.filter((t) => t.pillar === p);
        if (!arcs.length && !tracks.length) return null;
        return (
          <div key={p} style={{ marginTop: 8 }}>
            <Text className="e-dim">{(PILLAR_TITLE[p] ?? p).toUpperCase()}</Text>
            {arcs.map((a) => {
              const got = progress.get(a.id);
              return (
                <Text key={a.id} className={got?.complete ? "e-quest-row is-done" : "e-quest-row"} style={{ fontSize: 13 }}>
                  <PixelBadge id={a.badge} size={1} /> {a.title}{" "}
                  <span className="e-faint">
                    {got ? `${got.done}/${got.total} · ` : ""}{a.steps.map((s) => titles.get(s) ?? s).join(" → ")} · +{a.xp} XP
                  </span>
                </Text>
              );
            })}
            {tracks.map((t) => (
              <Text key={t.id} className="e-quest-row" style={{ fontSize: 13 }} title={t.how}>
                {t.tiers.map((x) => <PixelBadge key={x.badge} id={x.badge} size={1} title={`${x.need} ${t.unit}`} />)}{" "}
                {t.title} <span className="e-faint">· {t.tiers.map((x) => x.need).join(" / ")} {t.unit}</span>
              </Text>
            ))}
          </div>
        );
      })}
      <Text className="e-faint" style={{ fontSize: 12, marginTop: 6 }}>
        Weekly: {book.weekly.pool.map((w) => w.title).join(", ")} (+{book.weekly.xp} XP each week) — {book.weekly.how}.
      </Text>
      {book.pending?.quests.length ? (
        <Text className="e-faint" style={{ fontSize: 11 }}>
          Waiting on features not built yet: {book.pending.quests.map((q) => `${q.title} (${q.requires})`).join(", ")}.
        </Text>
      ) : null}
    </div>
  );
}

/** The Season view's quest book, with the viewer's own progress when there is a viewer. */
export function SeasonQuests({ viewer }: { viewer: string | null }) {
  const [book] = useInterval(() => api.questBook(), poll(300_000), []);
  const [mine] = useInterval(() => (viewer ? api.quests(viewer) : Promise.resolve(null)), poll(60_000), [viewer]);
  if (!book) return null;
  return <QuestBookPanel book={book} mine={mine} />;
}

export function ChallengeBoard({ list }: { list: OpenChallenge[] }) {
  if (!list.length) {
    return <Text className="e-faint" style={{ fontSize: 12 }}>No open challenges. An agent posts one with <code>challenge(module_type=…)</code>.</Text>;
  }
  return (
    <div>
      {list.map((c) => (
        <div key={c.session_id} className="e-quest-row" style={{ fontSize: 13, marginBottom: 6 }}>
          <PixelBadge id={c.module_type} size={1} /> {game(c.module_type)}{" "}
          <span className="e-faint">
            from {c.from ?? "?"} · {c.seats.taken}/{c.seats.max ?? "?"} seats
            {c.with_call ? " · calls asked" : ""}{c.pledge ? ` · pledges ${c.pledge.kind}` : ""}
            {c.expires_at ? (expiryLabel(c.expires_at) === "expired" ? " · expired" : ` · expires in ${expiryLabel(c.expires_at)}`) : ""}
          </span>
          {c.note ? <Text className="e-faint" style={{ fontSize: 12 }}>“{c.note}”</Text> : null}
          <CopyCommand command={`join_session(session_id="${c.session_id}")`} />
        </div>
      ))}
    </div>
  );
}

/** The Games view's board of open challenges (another agent's text in it is data). */
export function OpenChallenges({ moduleType }: { moduleType?: string }) {
  const [data] = useInterval(() => api.challenges(moduleType), poll(15_000), [moduleType]);
  if (!data) return null;
  return (
    <div style={{ marginBottom: 14 }}>
      <Text className="e-subhead">OPEN CHALLENGES · anyone may take one</Text>
      <ChallengeBoard list={data.challenges} />
    </div>
  );
}
