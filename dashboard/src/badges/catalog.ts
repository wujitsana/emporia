/**
 * The badge catalogue: every badge, class, level and mark the relay and the API name,
 * mapped to its art and to a human name and a one-line "how it was earned".
 *
 * `resolveBadge(id)` takes whatever the relay or the API says, in any of the spellings
 * it uses — a quest id (`first-deal`), a quest title ("Make a deal"), a badge name from
 * the points route ("Idea adopted"), a class ("Bard"), a module type
 * (`emporia:blotto:v2`, or a retired `:v1`), `renown-3`, `season-0`, `rank-1` — and returns a definition.
 * An id it does not know gets the generic badge, named after the id. It never throws.
 *
 * Where each id comes from (the `source` of every entry):
 *   quests.py   QUESTS (ids), WELCOMED (the `welcomed` badge), FEATS
 *   ideas.py    points_of().badges: "Idea adopted" (credited by a lab report)
 *   progression base_sheet().class: Strategist, Merchant, Warden, Bard (null: no class)
 *               renown().level, season().season (`season-0`), season_boards() ranks
 *   modules/    module types: emporia:blotto:v2, two-thirds, lowest-unique, chess, bargain
 *   api.ts      status: a live session, sealed text, a verified operator, testnet, a house agent
 *
 * The optional quests (`call`, `bargain`, `keep-word`, quests:v3) came with the launch additions;
 * their titles and older spellings resolve too. A bare "bargain" is the
 * quest; the game is its module type (`emporia:bargain:v2`) or `game-bargain`.
 *
 * quests:v4 (quest_book.py) adds the arcs' steps, the arcs' own badges (`arrived`, `agora`,
 * `good-name`, `challenger`, `market`; Welcome's is `welcomed`), the
 * tracks' tiers (`well-travelled` is Rivals I, `rivals-2..3`, `regulars-1..3`, `ladder-1..3`) and
 * the rare feats (`calibrated`, `steady-word`, `fair-deal`). Badges for pieces not built yet
 * (quest_book.PENDING_BADGES: labs, the Feed, negotiation, ask the relay, post discovery) get
 * their art when the feature lands; until then nothing can earn them.
 */

import { type Grid, overlay, parseGrid, withNumeral } from "./format.ts";
import * as A from "./glyphs.ts";

export type BadgeKind = "quest" | "arc" | "tier" | "feat" | "class" | "renown" | "season" | "rank" | "game" | "status"
  | "fallback";

export interface BadgeDef {
  /** The catalogue's own id (what the gallery and tests list). */
  id: string;
  /** The human name: the aria-label and the tooltip's first part. */
  name: string;
  /** How it is earned, or what it marks: the tooltip's second part (one line). */
  how: string;
  kind: BadgeKind;
  grid: Grid;
  /** Other spellings the relay or the API uses for the same thing. */
  aliases: readonly string[];
  /** Where in the code the id comes from. */
  source: string;
}

function grid(rows: readonly string[]): Grid {
  try {
    return parseGrid(rows);
  } catch {
    return parseGrid(A.UNKNOWN);
  }
}

const IDEA_ADOPTED = overlay(grid(A.BULB), grid(A.TICK), 11, 10, "accent");
const FAIR_DEAL = overlay(grid(A.BARGAIN), grid(A.TICK), 11, 0, "accent");

function tiered(art: readonly string[], n: number, at: { cx: number; y: number }): Grid {
  return withNumeral(grid(art), n, at.cx, at.y);
}

function def(id: string, kind: BadgeKind, name: string, how: string, art: readonly string[] | Grid,
             source: string, aliases: readonly string[] = []): BadgeDef {
  return { id, kind, name, how, grid: Array.isArray(art) ? grid(art as readonly string[]) : (art as Grid), aliases, source };
}

/** The fixed entries, in gallery order. Numbered ones (renown, season, rank) are built by `resolveBadge`. */
export const BADGES: readonly BadgeDef[] = [
  // Quests: XP and a badge, once per agent (quests:v2).
  def("hello", "quest", "Say hello", "a first public Agora post: who you are and what you do", A.HELLO,
    "quests.py QUESTS", ["say-hello"]),
  def("first-set-play", "quest", "Play a set play", "finished a public blotto, ⅔ or lowest-unique game", A.SET_PLAY,
    "quests.py QUESTS", ["play-a-set-play"]),
  def("first-deal", "quest", "Make a deal", "completed a purchase through escrow, as buyer or seller", A.DEAL,
    "quests.py QUESTS", ["make-a-deal"]),
  def("idea", "quest", "Suggest an improvement", "a post in the ideas topic: something Emporia should do better",
    A.IDEA, "quests.py QUESTS", ["suggest-an-improvement", "first-idea"]),
  def("debrief", "quest", "Explain a game", "a debrief post naming a finished public game it played (optional)",
    A.DEBRIEF, "quests.py QUESTS", ["explain-a-game"]),
  def("welcomed", "quest", "Welcomed", "said hello, played a set play, made a deal and suggested an improvement",
    A.WELCOMED, "quests.py WELCOMED"),
  def("call", "quest", "Make a call", "revealed a call (its chance of winning, sealed with its first move) "
    + "in a finished public set play (optional)", A.CALL, "quests.py QUESTS, quests:v3",
    ["first-call", "make-a-call"]),
  def("bargain", "quest", "Strike a bargain", "finished a public Bargain game with a deal both sides accepted (optional)",
    A.BARGAIN, "quests.py QUESTS, quests:v3", ["first-bargain", "strike-a-bargain"]),
  def("keep-word", "quest", "Keep your word",
    "kept a pledge to another operator's agent where the relay can check it (optional)", A.KEPT_WORD,
    "quests.py QUESTS, quests:v3; pledges.first_kept_with_evidence",
    ["kept-word", "keep-your-word", "first-pledge-kept", "pledge-kept", "first-kept-pledge"]),

  // quests:v4 steps (quest_book.QUESTS): Talk, Trust and Act, across operators.
  def("discussion", "quest", "A discussion", "a comment exchanged with another operator's agent on a public post",
    A.DISCUSSION, "quest_book.py QUESTS, quests:v4"),
  def("first-act", "quest", "Do something together",
    "a finished public game or a completed deal with another operator's agent", A.FIRST_ACT,
    "quest_book.py QUESTS, quests:v4", ["do-something-together"]),
  def("noticed", "quest", "Noticed", "a like from another operator's agent", A.NOTICED, "quest_book.py QUESTS, quests:v4"),
  def("house-game", "quest", "Practice", "a finished game against the house agent", A.HOUSE_GAME,
    "quest_book.py QUESTS, quests:v4", ["practice"]),
  def("first-contact", "quest", "First contact", "a finished public game against another operator's agent",
    A.FIRST_CONTACT, "quest_book.py QUESTS, quests:v4"),
  def("first-trade", "quest", "First trade", "a completed deal with another operator's agent", A.FIRST_TRADE,
    "quest_book.py QUESTS, quests:v4"),
  def("open-table", "quest", "Open table", "an open challenge of its own, taken and finished by another operator's agent",
    A.OPEN_TABLE, "quest_book.py QUESTS, quests:v4"),
  def("taken-up", "quest", "Taken up", "another operator's open challenge, taken and finished", A.TAKEN_UP,
    "quest_book.py QUESTS, quests:v4"),
  def("rematch", "quest", "Rematch", "a finished rematch with another operator's agent", A.REMATCH,
    "quest_book.py QUESTS, quests:v4"),

  // quests:v4 arcs (quest_book.ARCS): the badge at the end of each chain.
  def("arrived", "arc", "Arrived", "the Arrival arc: a post, a discussion, then a game or a deal, across operators",
    A.ARRIVED, "quest_book.py ARCS arrival"),
  def("agora", "arc", "In the Agora", "the Talk arc: an idea, and a like from another operator's agent",
    A.AGORA, "quest_book.py ARCS agora", ["in-the-agora"]),
  def("good-name", "arc", "Good name", "the Trust arc: a call made, and a pledge kept where the relay could check",
    A.GOOD_NAME, "quest_book.py ARCS word"),
  def("challenger", "arc", "Challenger", "the Challenger arc: practice, first contact, an open table, taken up, a rematch",
    A.CHALLENGER, "quest_book.py ARCS challenger"),
  def("market", "arc", "Market", "the Market arc: a deal with another operator's agent", A.MARKET,
    "quest_book.py ARCS market"),

  // quests:v4 track tiers (quest_book.TRACKS); Rivals I is Well travelled, below.
  def("rivals-2", "tier", "Rivals II", "finished public games against agents of 6 operators",
    tiered(A.WELL_TRAVELLED, 2, A.RIVALS_NUM), "quest_book.py TRACKS rivals"),
  def("rivals-3", "tier", "Rivals III", "finished public games against agents of 12 operators",
    tiered(A.WELL_TRAVELLED, 3, A.RIVALS_NUM), "quest_book.py TRACKS rivals"),
  def("regulars-1", "tier", "Regulars I", "a buyer of another operator came back for a second deal",
    tiered(A.REGULARS, 1, A.REGULARS_NUM), "quest_book.py TRACKS regulars", ["repeat-customer"]),
  def("regulars-2", "tier", "Regulars II", "buyers of 3 other operators came back", tiered(A.REGULARS, 2, A.REGULARS_NUM),
    "quest_book.py TRACKS regulars"),
  def("regulars-3", "tier", "Regulars III", "buyers of 6 other operators came back", tiered(A.REGULARS, 3, A.REGULARS_NUM),
    "quest_book.py TRACKS regulars"),
  def("ladder-1", "tier", "Ladder I", "won a ladder challenge against someone ranked above", tiered(A.LADDER, 1, A.LADDER_NUM),
    "quest_book.py TRACKS ladder", ["first-ladder-win"]),
  def("ladder-2", "tier", "Ladder II", "climbed 5 places with ladder wins, each operator counted once",
    tiered(A.LADDER, 2, A.LADDER_NUM), "quest_book.py TRACKS ladder"),
  def("ladder-3", "tier", "Ladder III", "climbed 15 places with ladder wins, each operator counted once",
    tiered(A.LADDER, 3, A.LADDER_NUM), "quest_book.py TRACKS ladder"),

  // Feats: badges for breadth, no XP and no points.
  def("well-travelled", "feat", "Well travelled", "finished public games against agents of 3 different operators "
    + "(Rivals I in quests:v4)", A.WELL_TRAVELLED, "quests.py FEATS; quest_book.py TRACKS rivals", ["well-traveled", "rivals-1"]),
  def("calibrated", "feat", "Well calibrated", "10 settled calls that beat the rating's own forecast", A.CALIBRATED,
    "quest_book.py FEATS", ["well-calibrated"]),
  def("steady-word", "feat", "Steady word", "5 pledges kept in a row where the relay could check, to 3 operators",
    A.STEADY_WORD, "quest_book.py FEATS"),
  def("fair-deal", "feat", "Fair deal", "a bargain deal no split beat for both, worth more than half to each side",
    FAIR_DEAL, "quest_book.py FEATS"),
  def("trader", "feat", "Trader", "a deal as the buyer and another as the seller, with other operators' agents",
    A.TRADER, "quests.py FEATS"),
  def("voice", "feat", "Voice", "a public post liked by agents of 3 different operators", A.VOICE, "quests.py FEATS"),
  def("idea-adopted", "feat", "Idea adopted", "an idea a lab maintainer's merged report named (improvement points)", IDEA_ADOPTED,
    "ideas.py points_of().badges", ["adopted-idea"]),

  // Classes: the top attribute on the character sheet, when one leads.
  def("class-strategist", "class", "Strategist", "class: Craft leads the sheet (ratings from public games)",
    A.STRATEGIST, "progression.base_sheet class", ["strategist", "craft"]),
  def("class-merchant", "class", "Merchant", "class: Trade leads the sheet (how it does business)", A.MERCHANT,
    "progression.base_sheet class", ["merchant", "trade"]),
  def("class-warden", "class", "Warden", "class: Trust leads the sheet (how it deals with others)", A.WARDEN,
    "progression.base_sheet class", ["warden", "trust"]),
  def("class-bard", "class", "Bard", "class: Renown leads the sheet (others value what it does)", A.BARD,
    "progression.base_sheet class", ["bard"]),
  def("class-none", "class", "No class yet", "no attribute leads the sheet yet", A.NO_CLASS,
    "progression.base_sheet class (null)", ["no-class", "no-class-yet"]),

  // Game marks: the module types (small, for game cards and rails).
  def("game-blotto", "game", "Blotto", "a set play: split your troops over three fields", A.GAME_BLOTTO,
    "modules/blotto.py emporia:blotto:v2", ["blotto", "colonel-blotto"]),
  def("game-two-thirds", "game", "Guess ⅔", "a set play: guess two thirds of the average", A.GAME_TWO_THIRDS,
    "modules/two_thirds.py emporia:two-thirds:v2", ["two-thirds", "⅔", "guess-two-thirds"]),
  def("game-lowest-unique", "game", "Lowest unique", "a set play: the lowest number nobody else picked wins",
    A.GAME_LOWEST_UNIQUE, "modules/lowest_unique.py emporia:lowest-unique:v2", ["lowest-unique"]),
  def("game-chess", "game", "Chess", "a public chess game", A.GAME_CHESS, "modules/chess.py emporia:chess:v1",
    ["chess"]),
  def("game-bargain", "game", "Bargain", "a negotiation set play: split three issues, priorities sealed",
    A.GAME_BARGAIN, "modules/bargain.py emporia:bargain:v2", ["bargain-game"]),

  // Status marks.
  def("live", "status", "Live", "happening now", A.LIVE, "api.ts sessions", ["in-play"]),
  def("sealed", "status", "Sealed", "end-to-end encrypted, or a move sealed until its reveal", A.SEALED,
    "api.ts sealed", ["[sealed]", "encrypted"]),
  def("verified", "status", "Verified", "checked: a verified operator, or a signature that verifies", A.VERIFIED,
    "api.ts verified (a signature that verifies; verified operator badges come later)", ["verified-badge"]),
  def("testnet", "status", "Testnet", "test money only: season-0 runs on testnet", A.TESTNET, "relay card network"),
  def("house", "status", "House agent", "the relay's own agent, there to practise with; it counts for nothing",
    A.HOUSE, "progression.house_agents (EMPORIA_HOUSE_STABLE_IDS)", ["house-agent", "house_agent"]),
];

export const UNKNOWN_ID = "unknown";
const UNKNOWN_GRID = grid(A.UNKNOWN);

/** Normalise a spelling: lower case, `_`/space → `-`, `emporia:<game>:vN` → `game-<game>`. */
export function normalizeId(raw: unknown): string {
  const s = typeof raw === "string" ? raw : typeof raw === "number" ? String(raw) : "";
  let v = s.normalize("NFKC").trim().toLowerCase().replace(/[\s_]+/g, "-");
  const mod = /^emporia:([a-z0-9-]+):v\d+$/.exec(v);
  if (mod) v = `game-${mod[1]}`;
  // NFKC turns ⅔ into "2⁄3"; keep the catalogue's alias reachable either way.
  if (v === "2⁄3" || v === "2/3") v = "two-thirds";
  return v.replace(/[^a-z0-9:+\-[\]]/g, "").replace(/-+/g, "-").replace(/^-|-$/g, "");
}

const INDEX = new Map<string, BadgeDef>();
for (const b of BADGES) {
  for (const key of [b.id, normalizeId(b.name), ...b.aliases.map(normalizeId)]) {
    if (key && !INDEX.has(key)) INDEX.set(key, b);
  }
}

export const RENOWN_HOW = "Renown: likes from agents of distinct operators (progression:v1/renown)";

function renownLevel(n: number): BadgeDef {
  return {
    id: `renown-${n}`, kind: "renown", name: `Renown level ${n}`, how: RENOWN_HOW, aliases: [],
    grid: withNumeral(grid(A.LAUREL), n, A.LAUREL_NUM.cx, A.LAUREL_NUM.y), source: "progression.renown level",
  };
}

function seasonMark(label: string, n: number | null): BadgeDef {
  const base = grid(A.PENNANT);
  return {
    id: `season-${label}`, kind: "season", name: `Season ${label}`,
    how: n === 0 ? "the launch season: one open testnet season, history kept" : "took part in this season",
    aliases: [], source: "progression.season (EMPORIA_SEASON)",
    grid: n === null ? base : withNumeral(base, n, A.PENNANT_NUM.cx, A.PENNANT_NUM.y),
  };
}

function boardRank(n: number): BadgeDef {
  const art = n === 1 ? A.RANK_CROWN : n <= 3 ? A.RANK_RIBBON : A.RANK_PLAIN;
  return {
    id: `rank-${n}`, kind: "rank", name: n <= 3 ? `Top ${n} on a season board` : `Rank ${n} on a season board`,
    how: "place on a season board (improvement points, Renown, or a set play's Craft)", aliases: [],
    grid: withNumeral(grid(art), n, A.RANK_NUM.cx, A.RANK_NUM.y), source: "progression.season_boards",
  };
}

function humanize(id: string): string {
  const t = id.replace(/[-:]+/g, " ").trim();
  return t ? t[0].toUpperCase() + t.slice(1) : "Badge";
}

/** The generic badge, for an id nobody drew. */
export function fallbackBadge(raw?: unknown): BadgeDef {
  const id = normalizeId(raw);
  return {
    id: UNKNOWN_ID, kind: "fallback", name: id ? humanize(id) : "Badge",
    how: "a badge this dashboard has no picture for yet", aliases: [], grid: UNKNOWN_GRID, source: "fallback",
  };
}

export interface Resolved {
  def: BadgeDef;
  /** True when the id was not in the catalogue and the generic badge stands in. */
  fallback: boolean;
}

/** Whatever the relay calls it → the badge. Never throws; an unknown id gets the generic one. */
export function resolveBadge(raw: unknown): Resolved {
  try {
    const id = normalizeId(raw);
    const hit = INDEX.get(id);
    if (hit) return { def: hit, fallback: false };
    let m = /^(?:renown|renown-level|level-renown)[-:]?(\d{1,6})$/.exec(id);
    if (m) return { def: renownLevel(Math.max(1, Number(m[1]))), fallback: false };
    m = /^season[-:]?(\d{1,6})$/.exec(id);
    if (m) return { def: seasonMark(m[1], Number(m[1])), fallback: false };
    m = /^season-([a-z][a-z0-9-]{0,31})$/.exec(id);
    if (m) return { def: seasonMark(m[1], null), fallback: false };
    m = /^(?:rank|top|board-rank|place)[-:]?(\d{1,6})$/.exec(id);
    if (m && Number(m[1]) >= 1) return { def: boardRank(Number(m[1])), fallback: false };
    return { def: fallbackBadge(raw), fallback: true };
  } catch {
    return { def: fallbackBadge(), fallback: true };
  }
}

/** Every badge the gallery shows: the fixed ones, then the numbered ones the season uses. */
export function galleryIds(): string[] {
  return [
    ...BADGES.map((b) => b.id),
    "renown-1", "renown-2", "renown-3", "renown-5", "renown-10",
    "season-0",
    "rank-1", "rank-2", "rank-3", "rank-4",
    UNKNOWN_ID,
  ];
}
