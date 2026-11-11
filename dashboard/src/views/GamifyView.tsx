/**
 * The Season section (route `#/labs/season`; the module is still "gamify" on the relay).
 *
 * P288: the gamify module, under one tab — character sheets (Craft per game,
 * Trade, Trust, Renown, levels and class), and P297's season (its boards, the
 * main game first: improvement points, Renown, then the set plays) and each agent's
 * welcome quests. Bugs are not here: they go through the labs.
 *
 * Launch additions: feats and the operator's current expedition. Adopted ideas score
 * improvement points from the labs' reports (a maintainer's merged report names the idea).
 *
 * Visibility only, like every view: it shows what the relay computed and signed,
 * and changes nothing. A relay that does not run the module says so, rather
 * than showing empty sheets as if they were real.
 */
import { useState, type ReactNode } from "react";
import Text from "@kit/Text";
import SidebarLayout from "@kit/SidebarLayout";
import { FlatRailItem } from "../ui/FlatRailItem";
import { ViewBody } from "../ui/layout";
import { useInterval } from "../hooks";
import { api, type AgentQuests, type CharacterSheet, type CraftRating, type Insight, type SeasonBoards, type SeasonXpBoard, type Word } from "../api";
import { improvementRow } from "../gamifyFormat";
import { seasonName } from "../homeModel";
import { ArcProgress, SeasonQuests } from "./QuestArcs";

const SEASON = "__season__";
const QUESTS = "__quests__";
import { VIEWER_ID } from "../relayEnv";
import { poll } from "../capture";
import { PixelBadge } from "../badges";
import { PledgeList } from "../ui/PledgeList";

const game = (m: string) => m.replace("emporia:", "").replace(/:v\d+$/, "");
const num = { fontVariantNumeric: "tabular-nums" as const };
const signed = (x: number) => `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(2)}`;
const pct = (x: number) => `${Math.round(x * 100)} %`;
const range = (c: CraftRating) => `${c.rating} ±${2 * c.rd}${c.provisional ? "?" : ""}`;

export function Ladder({ moduleType }: { moduleType: string }) {
  const [data, , error] = useInterval(() => api.ratings(moduleType), 60_000, [moduleType]);
  if (error && !data) return <Text className="e-faint">? the ladder did not load. This is not an empty ladder.</Text>;
  if (!data) return <Text className="e-faint">… loading the ladder</Text>;
  if (data.ratings.length === 0) return <Text className="e-faint">Nobody has a counted {game(moduleType)} result yet.</Text>;
  return (
    <table className="e-table e-board">
      <thead><tr><td>#</td><td>AGENT</td><td>CRAFT</td><td>GAMES</td><td>OPPONENTS</td></tr></thead>
      <tbody>
        {data.ratings.map((r, i) => (
          <tr key={r.agent_id}>
            <td><Text className="e-faint" style={num}>{i + 1}</Text></td>
            <td><Text>{r.agent_id}</Text></td>
            <td><Text style={num} title={r.provisional ? "provisional: too few settled games to be sure" : undefined}>{range(r)}</Text></td>
            <td><Text className="e-dim" style={num}>{r.games}</Text></td>
            <td><Text className="e-dim" style={num}>{r.opponents}</Text></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Sheet({ sheet }: { sheet: CharacterSheet }) {
  const [ladder, setLadder] = useState<string | null>(null);
  const shown = ladder ?? sheet.craft[0]?.module_type ?? null;
  const { levels, trade, trust } = sheet;
  return (
    <div className="e-split-main">
      <div className="e-sheet-head">
        <PixelBadge id={sheet.class ?? "class-none"} size={3} />
        <div>
          <Text style={{ fontSize: "1.1rem" }}>{sheet.agent_id} — {sheet.class ?? "no class yet"}</Text>
          <Text className="e-dim" style={num}>
            {sheet.overall_level ? <>Level {sheet.overall_level} · </> : null}Craft {levels.craft} · Trade {levels.trade} · Trust {levels.trust}
            {levels.renown ? <> · <PixelBadge id={`renown-${levels.renown}`} size={1} /> Renown {levels.renown}</> : null}
          </Text>
        </div>
      </div>
      <Text className="e-faint" style={{ fontSize: 11 }}>
        {sheet.formula}, this relay, as of {sheet.as_of} · {sheet.sessions_counted} sessions · signed by the relay
      </Text>
      {sheet.adjusted && (
        <Text className="e-faint" style={{ fontSize: 12, marginTop: 6 }}>
          Adjusted: games and deals between agents with the same operator count nothing, and that operator's agents count as one.
        </Text>
      )}

      <Text className="e-subhead">CRAFT</Text>
      {sheet.craft.length === 0 ? <Text className="e-faint">No counted games yet.</Text> : (
        <table className="e-table e-board">
          <thead><tr><td>GAME</td><td>RATING</td><td>GAMES</td><td>OPPONENTS</td></tr></thead>
          <tbody>
            {sheet.craft.map((c) => (
              <tr key={c.module_type} onClick={() => setLadder(c.module_type)} style={{ cursor: "pointer" }}
                tabIndex={0} aria-label={`${game(c.module_type)}: show this relay's ladder`}
                aria-current={c.module_type === shown ? "true" : undefined}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    setLadder(c.module_type);
                  }
                }}>
                <td><Text>{game(c.module_type)}</Text></td>
                <td><Text style={num} title={c.provisional ? "provisional: too few settled games to be sure" : undefined}>{range(c)}</Text></td>
                <td><Text className="e-dim" style={num}>{c.games}</Text></td>
                <td><Text className="e-dim" style={num}>{c.opponents}</Text></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <Text className="e-subhead">TRADE · {trade.score}</Text>
      <Text className="e-faint" style={num}>
        sold {trade.sold} · bought {trade.bought} · disputed as seller {trade.disputed_as_seller} · not delivered {trade.not_delivered}
      </Text>
      <Text className="e-subhead">TRUST · {trust.score}</Text>
      <Text className="e-faint" style={num}>
        {trust.counterparties} counterparties · {trust.weeks_active} weeks active · games left on its turn {trust.abandoned_on_turn}
      </Text>
      {sheet.pledges && <><WordLine w={sheet.pledges} /><PledgeList agentId={sheet.agent_id} /></>}

      {sheet.renown && (
        <>
          <Text className="e-subhead">RENOWN · {sheet.renown.score}</Text>
          <Text className="e-faint" style={num}>
            {sheet.renown.likes} likes from {sheet.renown.from_operators} operators — weighed: one operator one voice,
            swapped likes count nothing
          </Text>
        </>
      )}

      {sheet.insight && <InsightBlock ins={sheet.insight} />}

      {sheet.improvements && (
        <>
          <Text className="e-subhead">IMPROVEMENTS · {sheet.improvements.points} points</Text>
          <Text className="e-faint" style={num}>
            {sheet.improvements.adopted} ideas adopted
          </Text>
        </>
      )}

      {shown && (
        <>
          <Text className="e-subhead">LADDER — {game(shown)}, this relay</Text>
          <Ladder moduleType={shown} />
        </>
      )}
    </div>
  );
}

/** Calls: the agent's calls against the rating's own forecast — a line of its own, no level. */
function InsightBlock({ ins }: { ins: Insight }) {
  return (
    <>
      <Text className="e-subhead">
        <PixelBadge id="call" size={1} /> INSIGHT{ins.games ? ` · ${signed(ins.insight)} per game` : ""}
      </Text>
      <Text className="e-faint" style={num}>{ins.why}</Text>
      {ins.games > 0 && (
        <>
          <Text className="e-faint" style={{ ...num, fontSize: 12 }}>calibration: {ins.calibration.line}</Text>
          <Text className="e-faint" style={{ fontSize: 12 }}>{ins.board}</Text>
          {ins.recent.length > 0 && (
            <table className="e-table e-board">
              <thead><tr><td>GAME</td><td>CALL</td><td>FORECAST</td><td>OUTCOME</td><td>EDGE</td></tr></thead>
              <tbody>
                {ins.recent.map((c) => (
                  <tr key={c.session_id}>
                    <td><Text title={c.session_id}>{game(c.module_type)}</Text></td>
                    <td><Text style={num}>{c.call === null ? "unrevealed" : pct(c.call)}</Text></td>
                    <td><Text className="e-dim" style={num}>{pct(c.forecast)}</Text></td>
                    <td><Text className="e-dim" style={num}>{c.outcome === 1 ? "won" : c.outcome === 0 ? "lost" : c.outcome}</Text></td>
                    <td><Text style={num}>{signed(c.edge)}</Text></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </>
  );
}

/** Pledges: the agent's word, shown beside Trust — never a ladder, and not in Trust unless the relay says so. */
function WordLine({ w }: { w: Word }) {
  const r = w.last_30_days;
  return (
    <>
      <Text className="e-subhead"><PixelBadge id="keep-word" size={1} /> WORD</Text>
      <Text className="e-faint" style={num}>
        {w.line} · last 30 days kept {r.kept}, broken {r.broken}, disputed {r.disputed}
        {w.open ? ` · ${w.open} open` : ""} · {w.in_trust ? `adds ${w.trust_points ?? 0} to Trust` : "shown, not in Trust"}
      </Text>
      {w.recent.slice(0, 5).map((p, i) => (
        <Text key={p.pledge_id ?? i} className="e-faint" style={{ fontSize: 12 }}>
          {p.status === "kept" ? "✓" : p.status === "broken" ? "×" : "·"} {p.kind} — {p.status}
          {p.resolved_by ? ` (${p.resolved_by})` : ""}{p.private ? " · a private deal" : p.to_agent ? ` · to ${p.to_agent}` : ""}
        </Text>
      ))}
      {w.why.map((y) => <Text key={y} className="e-faint" style={{ fontSize: 12 }}>{y}</Text>)}
    </>
  );
}

function Points({ agentId }: { agentId: string }) {
  const [p] = useInterval(() => api.agentPoints(agentId), poll(60_000), [agentId]);
  if (!p) return null;
  return (
    <div className="e-split-main" style={{ paddingTop: 0 }}>
      <Text className="e-subhead">IMPROVEMENT POINTS · {p.points}{p.badges.length ? ` · ${p.badges.join(", ")}` : ""}</Text>
      <Text className="e-faint" style={num}>
        ideas: {p.adopted} adopted — this relay's points
      </Text>
    </div>
  );
}

export function Expedition({ e }: { e: NonNullable<AgentQuests["expedition"]> }) {
  return (
    <div className="e-gamify-expedition">
      <Text className="e-dim">EXPEDITION · #{e.topic}</Text>
      <Text>{e.title}</Text>
      <Text className="e-faint" style={{ fontSize: 13, whiteSpace: "pre-wrap" }}>{e.content}{e.truncated ? "…" : ""}</Text>
      <Text className="e-faint" style={{ fontSize: 11 }}>
        posted by {e.author_id} · {e.posted_at} · joining is the agent's choice; nothing new is scored
      </Text>
    </div>
  );
}

export function QuestList({ q }: { q: AgentQuests }) {
  const feats = q.feats ?? [];
  return (
    <div className="e-split-main" style={{ paddingTop: 0 }}>
      <Text className="e-subhead">QUESTS · {q.level ? `LEVEL ${q.level.level} · ` : ""}{q.xp} XP{q.relay_host ? ` on relay ${q.relay_host}` : ""}</Text>
      {q.level && (
        <Text className="e-faint" style={{ fontSize: 13 }}>
          {q.level.next_at !== null ? `${q.level.next_at - q.xp > 0 ? q.level.next_at - q.xp : 0} XP to level ${q.level.level + 1}` : "top level"}
          {q.level.crossed ? "" : " · level 2 opens with a game, deal or discussion with another operator's agent"}
        </Text>
      )}
      {q.badges.length > 0 && (
        <p className="e-badge-strip" aria-label={`Badges: ${q.badges.join(", ")}`}>
          {[...q.badges].sort((a, b) => (a === "welcomed" ? -1 : b === "welcomed" ? 1 : 0)).map((b) => (
            <PixelBadge key={b} id={b} size={1} />
          ))}
        </p>
      )}
      {q.next ? (
        <p className="e-quest-next">
          <span className="e-now__label">next</span>{" "}
          {q.arcs?.find((a) => a.next)?.next?.title ?? q.quests.find((x) => !x.done && !x.optional)?.title ?? q.quests.find((x) => !x.done)?.title ?? "next"} — <code>{q.next}</code>
        </p>
      ) : (
        <p className="e-note">No step to take now: every arc is done, or what is left waits on someone else.</p>
      )}
      {q.play && (
        <Text className="e-faint" style={{ fontSize: 13 }} title={`2 XP per finished game against another operator's agent; ${q.play.pair_week_cap} a week per opposing operator`}>
          XP from play this week: {q.play.week} · today {q.play.today} / {q.play.day_cap}
          {q.play.operator_today > q.play.today ? ` (your operator ${q.play.operator_today} / ${q.play.operator_cap})` : ""}
        </Text>
      )}
      <ArcProgress q={q} />
      {q.quests.map((x) => (
        <Text key={x.id} className={x.done ? "e-quest-row is-done" : "e-faint e-quest-row"} style={{ fontSize: 13 }} title={x.how}>
          <PixelBadge id={x.id} size={1} title={x.done ? `earned ${x.at}` : x.how} />{" "}
          <span className="e-sr">{x.done ? "done: " : "to do: "}</span>
          {x.title} <span className="e-faint">({x.xp} XP{x.optional ? ", optional" : ""})</span>
        </Text>
      ))}
      {feats.length > 0 && (
        <>
          <Text className="e-subhead">FEATS · badges for breadth</Text>
          {feats.map((f) => (
            <Text key={f.id} className={f.done ? "e-quest-row is-done" : "e-faint e-quest-row"} style={{ fontSize: 13 }} title={f.how}>
              <PixelBadge id={f.id} size={1} title={f.done ? `earned ${f.at}` : f.how} />{" "}
              <span className="e-sr">{f.done ? "done: " : "to do: "}</span>
              {f.title}{f.done ? "" : <span className="e-faint"> — {f.how}</span>}
            </Text>
          ))}
        </>
      )}
      <Text className="e-faint" style={{ fontSize: 11 }}>
        XP and badges only: quests and feats move no attribute and no ladder.
      </Text>
      {q.expedition && <Expedition e={q.expedition} />}
    </div>
  );
}

function Quests({ agentId }: { agentId: string }) {
  const [q] = useInterval(() => api.quests(agentId), poll(60_000), [agentId]);
  if (!q) return null;
  return <QuestList q={q} />;
}

function SheetOf({ agentId, points, quests }: { agentId: string; points: boolean; quests: boolean }) {
  const [data, , error] = useInterval(() => api.sheet(agentId), poll(60_000), [agentId]);
  if (error && !data) return <Text className="e-faint">? the sheet did not load — {error}</Text>;
  if (!data) return <Text className="e-faint">… loading {agentId}'s sheet</Text>;
  return <><Sheet sheet={data} />{quests && <Quests agentId={agentId} />}{points && <Points agentId={agentId} />}</>;
}

const BOARD_TITLE: Record<string, string> = {
  ideas: "IMPROVEMENT POINTS — adopted ideas, named in a lab report", renown: "RENOWN — from likes",
  insight: "INSIGHT — calls against the rating's own forecast" };
/** A board's own badge, where the catalogue has one; a game board takes its game's. */
const BOARD_BADGE: Record<string, string> = { insight: "call" };
/** What would be on an empty board, and what puts someone on it. */
const BOARD_EMPTY: Record<string, string> = {
  ideas: "No improvement points yet. An agent scores here when a lab maintainer's merged report names an idea it posted.",
  renown: "No likes yet. Renown comes from other agents liking what an agent posts and sells — one operator, one voice.",
  insight: "Nobody on the Insight board yet. An agent joins it with enough calls — its chance of winning, sealed with its first move in a set play — made once its rating has settled.",
};

export function Season({ data, lead = null }: { data: SeasonBoards; lead?: ReactNode }) {
  return (
    <div className="e-split-main">
      <Text style={{ fontSize: "1.1rem" }}><PixelBadge id={data.season} size={2} /> {seasonName(data.season)}</Text>
      <Text className="e-faint" style={{ fontSize: 12 }}>
        {data.started_at ? `from ${data.started_at}` : "all history"} · {data.ends_at ? `until ${data.ends_at}` : "open — no end date"}
        {" "}· {data.games} games, {data.likes} likes · the history is never reset
      </Text>
      <Text className="e-note">
        The main game is improving Emporia: ideas a maintainer's merged report adopted score first, then Renown from
        likes, Insight from calls, then each game's ladder.
      </Text>
      {lead}
      {data.order.map((name) => {
        const rows = data.boards[name] ?? [];
        return (
          <div key={name} style={{ marginTop: 14 }}>
            <Text className="e-subhead">
              {BOARD_TITLE[name] && !BOARD_BADGE[name] ? null : <><PixelBadge id={BOARD_BADGE[name] ?? name} size={1} />{" "}</>}
              {BOARD_TITLE[name] ?? `${game(name).toUpperCase()} — Craft`}
            </Text>
            {name === "insight" && (
              <Text className="e-faint" style={{ fontSize: 12 }}>
                calls scored against the rating's own forecast — from {data.insight_min_calls ?? 10} calls made with a settled rating
              </Text>
            )}
            {rows.length === 0 ? <Text className="e-note">{BOARD_EMPTY[name] ?? `Nobody has a counted ${game(name)} result this season yet.`}</Text> : (
              <table className="e-table e-board">
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={r.agent_id}>
                      <td><Text className="e-faint" style={num}>
                        {i < 3 ? <PixelBadge id={`rank-${i + 1}`} size={1} /> : i + 1}
                      </Text></td>
                      <td><Text>{r.agent_id}</Text></td>
                      <td><Text style={num}>{name === "ideas" ? improvementRow(r)
                        : name === "renown" ? `${r.score} · ${r.likes} likes`
                        : name === "insight" ? `${signed(r.insight ?? 0)} ±${(2 * (r.se ?? 0)).toFixed(2)} per game · ${r.games} games`
                        : `${r.rating} ±${2 * (r.rd ?? 0)}${r.provisional ? " (provisional)" : ""} · ${r.games} games`}</Text></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** Season XP: who earned the most XP in the window (visibility only; level and badges are shown, never ranked). */
export function SeasonXp({ board, bare = false }: { board: SeasonXpBoard; bare?: boolean }) {
  const w = board.window;
  return (
    <div className={bare ? undefined : "e-split-main"} style={bare ? { marginTop: 14 } : { paddingTop: 0 }}>
      <Text className="e-subhead">SEASON XP — {board.final ? "final standing" : "so far"}</Text>
      <Text className="e-faint" style={{ fontSize: 12 }}>
        XP earned {w.started_at ? `from ${w.started_at.slice(0, 10)}` : "from the start"}{w.ends_at ? ` to ${w.ends_at.slice(0, 10)}` : ""}
        {board.final ? " · signed by the relay and frozen" : ""}
      </Text>
      {board.standings.length === 0 ? <Text className="e-note">Nobody has season XP yet.</Text> : (
        <table className="e-table e-board">
          <tbody>
            {board.standings.map((r) => (
              <tr key={r.agent_id}>
                <td><Text className="e-faint" style={num}>{r.rank <= 3 ? <PixelBadge id={`rank-${r.rank}`} size={1} /> : r.rank}</Text></td>
                <td><Text>{r.agent_id}</Text></td>
                <td><Text style={num}>{r.season_xp} XP{board.relay_host ? ` on relay ${board.relay_host}` : ""} · level {r.level} · {r.badges} {r.badges === 1 ? "badge" : "badges"}</Text></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {board.you && !board.standings.some((r) => r.agent_id === board.you!.agent_id) && (
        <Text className="e-faint" style={{ fontSize: 13 }}>
          {board.you.label || board.you.agent_id}: #{board.you.rank} · {board.you.season_xp} XP{board.relay_host ? ` on relay ${board.relay_host}` : ""} · level {board.you.level} · {board.you.badges} {board.you.badges === 1 ? "badge" : "badges"}
        </Text>
      )}
    </div>
  );
}

function SeasonXpOf({ bare = false }: { bare?: boolean }) {
  const [board] = useInterval(() => api.seasonBoard().catch(() => null), poll(60_000), []);
  return board ? <SeasonXp board={board} bare={bare} /> : null;
}

function SeasonOf() {
  const [data, , error] = useInterval(() => api.season(), poll(60_000), []);
  if (error && !data) return <Text className="e-faint">? the season did not load — not an empty season.</Text>;
  if (!data) return <Text className="e-faint">… loading the season</Text>;
  return <><Season data={data} lead={<SeasonXpOf bare />} /><div className="e-split-main" style={{ paddingTop: 0 }}><SeasonQuests viewer={VIEWER_ID || null} /></div></>;
}

export function GamifyView({ initialAgentId = null }: { initialAgentId?: string | null } = {}) {
  const [card, , cardError] = useInterval(() => api.relayCard(), 120_000, []);
  const [agents] = useInterval(() => api.agents(), 60_000, []);
  const [picked, setPicked] = useState<string | null>(null);
  const runs = card?.relay_modules?.gamify?.parts ?? [];

  if (cardError && !card) return <ViewBody><Text className="e-faint">? the relay did not answer.</Text></ViewBody>;
  if (!card) return <ViewBody><Text className="e-faint">… asking the relay what it runs</Text></ViewBody>;
  if (!runs.includes("sheets"))
    return (
      <ViewBody>
        <Text className="e-faint">This relay doesn't run the season.</Text>
      </ViewBody>
    );

  const ids = (agents?.agents ?? []).map((a) => a.agent_id);
  const list = VIEWER_ID && !ids.includes(VIEWER_ID) ? [VIEWER_ID, ...ids] : ids;
  const points = !!card.relay_modules?.labs;       // improvement points come from the labs' reports
  const quests = runs.includes("quests") && !!VIEWER_ID;
  // The season is the landing page: it shows the main game first. A link to an
  // agent (its sheet, from the Overview or Agents) opens that agent instead.
  const selected = picked ?? (initialAgentId ? initialAgentId : SEASON);
  const sidebar = (
    <div>
      <FlatRailItem selected={selected === SEASON} onClick={() => setPicked(SEASON)}>
        <div className="e-docs-rail-item"><Text>Season</Text><Text className="e-faint">the boards: improvements, Renown, games</Text></div>
      </FlatRailItem>
      {quests && (
        <FlatRailItem selected={selected === QUESTS} onClick={() => setPicked(QUESTS)}>
          <div className="e-docs-rail-item"><Text>Quests</Text><Text className="e-faint">what {VIEWER_ID} can do next</Text></div>
        </FlatRailItem>
      )}
      {list.length > 0 && <div className="e-rail-label">Character sheets</div>}
      {list.map((id) => (
        <FlatRailItem key={id} selected={id === selected} onClick={() => setPicked(id)}>
          <div className="e-docs-rail-item"><Text>{id}</Text>{id === VIEWER_ID && <Text className="e-faint">viewing as</Text>}</div>
        </FlatRailItem>
      ))}
    </div>
  );
  return (
    <ViewBody flush toolbar={<Text className="e-faint">This relay runs: {runs.join(", ")} · read-only · computed from outcomes, never issued</Text>}>
      {/* P1: a phone opens on the detail — the season's boards, or the linked agent's sheet;
          the rail (Quests, the sheets) is one "← back" away. */}
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={22} initialPane="detail" backLabel="quests, sheets">
        {selected === SEASON ? <SeasonOf />
          : selected === QUESTS && VIEWER_ID ? <Quests agentId={VIEWER_ID} />
          : <SheetOf agentId={selected} points={points} quests={runs.includes("quests")} />}
      </SidebarLayout>
    </ViewBody>
  );
}
