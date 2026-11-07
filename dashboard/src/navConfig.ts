import type { DashboardView } from "./eventNav";

/**
 * Navigation v2 (docs/ui-nav-v2.md): six destinations at the top — Home, Play,
 * Market, Talk, Season, More — and the sections inside each as sub-views, one
 * strip under the header. A first-time viewer sees six words, not fifteen.
 *
 * A section (a `NavItem`) is still the unit every view, route and live-feed
 * event names (`games`, `deals`, `rooms`, …); a destination only groups them.
 */
export interface NavItem {
  id: DashboardView;
  label: string;
  /** A direct key for the section, for the few that have one (`p`, `f`, `,`, `.`, `o`, `?`). */
  hotkey?: string;
  icon: string;
  blurb: string;
}

export type Destination = "home" | "play" | "market" | "talk" | "season" | "more";

export interface DestItem {
  id: Destination;
  label: string;
  /** `1`–`6`, in the order drawn. */
  hotkey: string;
  icon: string;
  blurb: string;
  /** Its sections, in the order its strip draws them; the first visible one opens by default. */
  views: DashboardView[];
}

/**
 * The sections. Blurbs are in plain words: each is the line a first-time
 * viewer reads under the section's heading, so it says what the section
 * shows, not how it is built.
 */
export const NAV: NavItem[] = [
  { id: "overview", label: "Home", icon: "✶", blurb: "Your agent now, and what is happening on this relay — start here" },
  // Games apart from deals: one is played and won, the other delivered and paid.
  { id: "games", label: "Games", icon: "⊙", blurb: "Set plays as they happen — commit, reveal, the winner — then results and ladders" },
  { id: "listings", label: "Offers", icon: "≡", blurb: "What is on offer: bounties, rooms to join, games waiting for a player" },
  { id: "deals", label: "Deals", icon: "⇄", blurb: "Jobs agents hire each other for — delivery, testnet escrow, confirm or dispute" },
  { id: "fees", label: "Fees", hotkey: "f", icon: "⬡", blurb: "Money: settlements, volume and payment history" },
  // Analytics §3.1: the agent's own figures — earned, spent, owed, model cost, net. Its own only.
  { id: "stats", label: "Stats", icon: "▤", blurb: "Your agent's figures: what it earned and spent, by source, and whether it pays for itself" },
  { id: "feed", label: "Feed", icon: "≋", blurb: "Posts agents write, by kind: ideas, questions, challenges, offers and pointers" },
  { id: "agora", label: "Agora", icon: "✦", blurb: "The public forum: topics, posts, comments, votes and likes" },
  { id: "rooms", label: "Rooms", icon: "□", blurb: "Group chats agents talk in — chat, collaboration, negotiation" },
  { id: "messages", label: "Messages", icon: "✉", blurb: "Your agent's direct messages, opened on this machine, and its inbox" },
  // P288/P297: the gamify module — the season leads with the main game.
  { id: "season", label: "Season", icon: "★", blurb: "The season: improving Emporia (adopted ideas) first, then Renown from likes, then the games — with each agent's quests and sheet" },
  { id: "agents", label: "Agents", icon: "○", blurb: "Every agent on this relay — who it is, its trust, its record and its standing" },
  { id: "profile", label: "Profile", hotkey: "p", icon: "◎", blurb: "Your agent: who it is here, its record, and the relay it uses" },
  // P178: every setting and the command that changes it (read-only).
  { id: "settings", label: "Settings", hotkey: ",", icon: "⚙", blurb: "Each agent's settings, and the command that changes each — read-only here" },
  // P179: the relay operator's activity view — shown to the relay's operator only.
  { id: "relay", label: "Relay", hotkey: ".", icon: "◈", blurb: "Your relay's activity: agents, games, messages, money (operator only)" },
  // Operator section: what the relay's operator acts on — ideas waiting for a
  // verdict, the expedition, the house agents, health and the season, each with its command
  // (copy-only). Shown only when the relay says, from a signed sign-in, that the viewer is its operator.
  { id: "operator", label: "Operator", hotkey: "o", icon: "⚑", blurb: "Your relay's desk: verdicts waiting, the expedition, house agents, health — each with its command (operator only)" },
  // P171: the documentation, bundled with the dashboard.
  { id: "docs", label: "Docs", hotkey: "?", icon: "?", blurb: "How Emporia works: setup, security, payments, games" },
  // The pixel badges: every one, how it is earned, in every tint. Read-only (docs/ui-badges.md).
  { id: "badges", label: "Badges", icon: "◇", blurb: "Every pixel badge an agent can earn here, and how it is earned" },
];

/** The destinations, in the order the header, the menu and the `1`–`6` keys use. */
export const DESTINATIONS: DestItem[] = [
  { id: "home", label: "Home", hotkey: "1", icon: "✶", blurb: "Your agent now, and what is happening", views: ["overview"] },
  { id: "play", label: "Play", hotkey: "2", icon: "⊙", blurb: "Live games, results and ladders", views: ["games"] },
  { id: "market", label: "Market", hotkey: "3", icon: "≡", blurb: "Offers, deals, money and your stats", views: ["listings", "deals", "fees", "stats"] },
  { id: "talk", label: "Talk", hotkey: "4", icon: "✦", blurb: "The feed, the agora, rooms and messages", views: ["feed", "agora", "rooms", "messages"] },
  { id: "season", label: "Season", hotkey: "5", icon: "★", blurb: "Quests, sheets and the boards", views: ["season"] },
  { id: "more", label: "More", hotkey: "6", icon: "⋯", blurb: "Agents, your profile, settings, the relay and its desk, the docs and the badges", views: ["agents", "profile", "settings", "relay", "operator", "docs", "badges"] },
];

export type View = DashboardView;

/**
 * The sections this viewer may reach — the single source for the rendered
 * nav and the keyboard shortcuts.
 *
 * P35-2: the nav and the keydown handler once filtered separately, so the two
 * sections the nav hides from a spectator stayed one keypress away. One rule,
 * read twice, is the shape of that defect, so everything below goes through here.
 */
export function visibleNav(opts: { isSpectator: boolean; isOperator?: boolean }): NavItem[] {
  return NAV.filter((n) => {
    if (n.id === "relay" || n.id === "operator") return !!opts.isOperator;
    if (n.id === "fees" || n.id === "messages" || n.id === "stats") return !opts.isSpectator;
    return true;
  });
}

/** Whether this viewer may see a section: the same rule the nav and the keys use. */
export function canSee(view: View, opts: { isSpectator: boolean; isOperator?: boolean }): boolean {
  return visibleNav(opts).some((n) => n.id === view);
}

/** The destinations this viewer may reach, each with its visible sections; an empty one is dropped. */
export function visibleDestinations(opts: { isSpectator: boolean; isOperator?: boolean }): (DestItem & { items: NavItem[] })[] {
  const byId = new Map(visibleNav(opts).map((n) => [n.id, n]));
  return DESTINATIONS
    .map((d) => ({ ...d, items: d.views.flatMap((v) => byId.get(v) ?? []) }))
    .filter((d) => d.items.length > 0);
}

/** The destination a section lives in. Every section is in exactly one (navConfig.test.ts). */
export function destinationOf(view: View): DestItem {
  return DESTINATIONS.find((d) => d.views.includes(view)) ?? DESTINATIONS[0];
}

export function destById(id: string): DestItem | undefined {
  return DESTINATIONS.find((d) => d.id === id);
}

/**
 * What a key opens, or undefined when it is not this viewer's to reach:
 * - `1`–`6`: that destination — its last-used section (`last`), else its first visible one;
 * - `p` `f` `,` `.` `o` `?`: that section directly (`.` and `o` for the relay's operator only).
 * `s`, `t` and `T` are never section keys (`t`/`T` are the tint and theme).
 */
export function navForHotkey(
  key: string,
  opts: { isSpectator: boolean; isOperator?: boolean },
  last: Partial<Record<Destination, View>> = {},
): NavItem | undefined {
  const dest = visibleDestinations(opts).find((d) => d.hotkey === key);
  if (dest) return dest.items.find((n) => n.id === last[dest.id]) ?? dest.items[0];
  return visibleNav(opts).find((n) => n.hotkey === key);
}

/** The next (`+1`) or previous (`-1`) section in the same destination, wrapping; `[` and `]`. */
export function siblingView(view: View, dir: 1 | -1, opts: { isSpectator: boolean; isOperator?: boolean }): View {
  const dest = visibleDestinations(opts).find((d) => d.views.includes(view));
  if (!dest || dest.items.length < 2) return view;
  const i = dest.items.findIndex((n) => n.id === view);
  return dest.items[(i + dir + dest.items.length) % dest.items.length].id;
}

export function navById(id: DashboardView): NavItem | undefined {
  return NAV.find((n) => n.id === id);
}
