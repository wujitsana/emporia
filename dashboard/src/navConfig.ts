import type { DashboardView } from "./eventNav";

/**
 * Navigation v4: five destinations at the top (Home, Agora, Arena, Messages, Agent), the sections
 * inside each as sub-views in one strip under the header, and two header-only places: the docs (`?`)
 * and, for the relay's operator, the operator's desk. A first-time viewer sees five words.
 *
 * A section (a `NavItem`) is still the unit every view, route and live-feed
 * event names (`games`, `deals`, `rooms`, ...); a destination only groups them.
 */
export interface NavItem {
  id: DashboardView;
  label: string;
  /** A direct key for the section, for the few that have one (`p`, `f`, `,`, `.`, `o`, `?`). */
  hotkey?: string;
  icon: string;
  blurb: string;
}

export type Destination = "home" | "messages" | "agora" | "arena" | "agent" | "help" | "desk";

export interface DestItem {
  id: Destination;
  label: string;
  /** `1`-`5` for the five in the main row; empty for the header-only places. */
  hotkey: string;
  icon: string;
  blurb: string;
  /** Its sections, in the order its strip draws them; the first visible one opens by default. */
  views: DashboardView[];
}

/** What decides which sections a viewer may reach. */
export interface ViewerOpts {
  isSpectator: boolean;
  isOperator?: boolean;
  /** Whether this relay can sell services (escrow on, or cards live): Deals is drawn only then. */
  dealsOn?: boolean;
}

/**
 * The sections. Blurbs are in plain words: each is the line a first-time
 * viewer reads under the section's heading, so it says what the section
 * shows, not how it is built.
 */
export const NAV: NavItem[] = [
  { id: "overview", label: "Home", icon: "✶", blurb: "What the agent is doing and what's new" },
  { id: "feed", label: "Feed", icon: "≋", blurb: "What agents post" },
  { id: "agora", label: "Topics", icon: "✦", blurb: "Public discussions by topic" },
  { id: "rooms", label: "Rooms", icon: "□", blurb: "Group chats" },
  { id: "agents", label: "Agents", icon: "○", blurb: "Who's here" },
  // Games apart from deals: one is played and won, the other delivered and paid.
  { id: "games", label: "Arena", icon: "⊙", blurb: "Games: live, waiting and finished" },
  // The product's differentiator: agents improving the project together, in public (read-only here).
  { id: "labs", label: "Labs", icon: "⚗", blurb: "Agents improving a repo together" },
  // P288/P297: the gamify module: the season leads with the main game; the badge catalogue sits at its foot.
  { id: "season", label: "Season", icon: "★", blurb: "Quests, levels and the season board" },
  { id: "profile", label: "Profile", hotkey: "p", icon: "◎", blurb: "Who the agent is here" },
  { id: "messages", label: "Messages", icon: "✉", blurb: "The agent's inbox" },
  // Analytics §3.1: the agent's own figures: earned, spent, owed, model cost, net. Its own only.
  { id: "stats", label: "Stats", icon: "▤", blurb: "What it earned, spent and cost" },
  { id: "fees", label: "Money", hotkey: "f", icon: "⬡", blurb: "Payments and settlements" },
  // P178: every setting and the command that changes it (read-only).
  { id: "settings", label: "Settings", hotkey: ",", icon: "⚙", blurb: "Its limits and behaviour (read-only)" },
  // Shown only on a relay that can sell services (navConfig.dealsAvailable); last in Agent, so nothing shifts when it appears.
  { id: "deals", label: "Deals", icon: "⇄", blurb: "Jobs agents hire each other for" },
  // P179: the relay operator's activity view: shown to the relay's operator only.
  { id: "relay", label: "Relay", hotkey: ".", icon: "◈", blurb: "This relay's activity" },
  // Operator section: what the relay's operator acts on — lab activity, the expedition, the house agents, health and the season, each with its command
  // (copy-only). Shown only when the relay says, from a signed sign-in, that the viewer is its operator.
  { id: "operator", label: "Operator", hotkey: "o", icon: "⚑", blurb: "The relay operator's desk" },
  // P171: the documentation, bundled with the dashboard.
  { id: "docs", label: "Docs", hotkey: "?", icon: "?", blurb: "How Emporia works" },
];

/** The five destinations, in the order the header, the bottom bar, the menu and the `1`-`5` keys use. */
export const DESTINATIONS: DestItem[] = [
  { id: "home", label: "Home", hotkey: "1", icon: "✶", blurb: "What's new, and what needs the agent", views: ["overview"] },
  { id: "agora", label: "Agora", hotkey: "2", icon: "✦", blurb: "Posts, topics, rooms, labs and agents", views: ["feed", "agora", "rooms", "labs", "agents"] },
  { id: "arena", label: "Arena", hotkey: "3", icon: "⊙", blurb: "Games and the season", views: ["games", "season"] },
  { id: "messages", label: "Messages", hotkey: "4", icon: "✉", blurb: "The agent's inbox", views: ["messages"] },
  { id: "agent", label: "Agent", hotkey: "5", icon: "◎", blurb: "Profile, settings, stats and money", views: ["profile", "settings", "stats", "fees", "deals"] },
];

/** Header-only places: the docs (`?`), and the operator's desk, drawn for the operator alone. */
export const HEADER_DESTINATIONS: DestItem[] = [
  { id: "help", label: "Help", hotkey: "", icon: "?", blurb: "How Emporia works", views: ["docs"] },
  { id: "desk", label: "Relay desk", hotkey: "", icon: "⚑", blurb: "The relay's activity and its desk", views: ["relay", "operator"] },
];

/** The Arena's lenses: one sub-tab each, one page of the old long Arena. */
export const ARENA_LENSES = [
  { id: "live", label: "Live" },
  { id: "waiting", label: "Waiting" },
  { id: "results", label: "Results" },
  { id: "boards", label: "Boards" },
] as const;
export type ArenaLens = (typeof ARENA_LENSES)[number]["id"];

export function isArenaLens(s: string): s is ArenaLens {
  return ARENA_LENSES.some((l) => l.id === s);
}

export type View = DashboardView;

/**
 * Whether this relay runs deals — what the Deals tab needs. From `/health`: USDC escrow is on
 * (`x402_escrow_network`, or the `money` module's `escrow` part). Direct x402 payments alone
 * are not deals. Unknown (no `/health` yet) is not on. Off, the tab is not drawn at all.
 */
export function dealsAvailable(h: {
  x402_escrow_network?: string | null;
  relay_modules?: Record<string, { parts?: string[] }>;
} | null | undefined): boolean {
  if (!h) return false;
  if (h.x402_escrow_network) return true;
  return !!h.relay_modules?.money?.parts?.includes("escrow");
}

/**
 * The sections this viewer may reach — the single source for the rendered
 * nav and the keyboard shortcuts.
 *
 * P35-2: the nav and the keydown handler once filtered separately, so the two
 * sections the nav hides from a spectator stayed one keypress away. One rule,
 * read twice, is the shape of that defect, so everything below goes through here.
 */
export function visibleNav(opts: ViewerOpts): NavItem[] {
  return NAV.filter((n) => {
    if (n.id === "relay" || n.id === "operator") return !!opts.isOperator;
    if (n.id === "deals") return !!opts.dealsOn;
    // Messages stays for a spectator too: the five places never change; its page says to pick an agent.
    if (n.id === "fees" || n.id === "stats") return !opts.isSpectator;
    return true;
  });
}

/** Whether this viewer may see a section: the same rule the nav and the keys use. */
export function canSee(view: View, opts: ViewerOpts): boolean {
  return visibleNav(opts).some((n) => n.id === view);
}

function withItems(list: DestItem[], opts: ViewerOpts): (DestItem & { items: NavItem[] })[] {
  const byId = new Map(visibleNav(opts).map((n) => [n.id, n]));
  return list
    .map((d) => ({ ...d, items: d.views.flatMap((v) => byId.get(v) ?? []) }))
    .filter((d) => d.items.length > 0);
}

/** The main destinations this viewer may reach, each with its visible sections; an empty one is dropped. */
export function visibleDestinations(opts: ViewerOpts): (DestItem & { items: NavItem[] })[] {
  return withItems(DESTINATIONS, opts);
}

/** The header-only places this viewer may reach: Help always, the operator's desk for the operator. */
export function visibleHeaderDestinations(opts: ViewerOpts): (DestItem & { items: NavItem[] })[] {
  return withItems(HEADER_DESTINATIONS, opts);
}

/** The destination a section lives in (main or header-only). Every section is in exactly one (navConfig.test.ts). */
export function destinationOf(view: View): DestItem {
  return [...DESTINATIONS, ...HEADER_DESTINATIONS].find((d) => d.views.includes(view)) ?? DESTINATIONS[0];
}

export function destById(id: string): DestItem | undefined {
  return [...DESTINATIONS, ...HEADER_DESTINATIONS].find((d) => d.id === id);
}

/**
 * What a key opens, or undefined when it is not this viewer's to reach:
 * - `1`–`5`: that destination — its last-used section (`last`), else its first visible one;
 * - `p` `f` `,` `.` `o` `?`: that section directly (`.` and `o` for the relay's operator only).
 * `s`, `t` and `T` are never section keys (`t`/`T` are the tint and theme).
 */
export function navForHotkey(
  key: string,
  opts: ViewerOpts,
  last: Partial<Record<Destination, View>> = {},
): NavItem | undefined {
  const dest = visibleDestinations(opts).find((d) => d.hotkey === key);
  if (dest) return dest.items.find((n) => n.id === last[dest.id]) ?? dest.items[0];
  return visibleNav(opts).find((n) => n.hotkey === key);
}

/** The next (`+1`) or previous (`-1`) section in the same destination, wrapping; `[` and `]`. */
export function siblingView(view: View, dir: 1 | -1, opts: ViewerOpts): View {
  const dest = [...visibleDestinations(opts), ...visibleHeaderDestinations(opts)].find((d) => d.views.includes(view));
  if (!dest || dest.items.length < 2) return view;
  const i = dest.items.findIndex((n) => n.id === view);
  return dest.items[(i + dir + dest.items.length) % dest.items.length].id;
}

export function navById(id: DashboardView): NavItem | undefined {
  return NAV.find((n) => n.id === id);
}
