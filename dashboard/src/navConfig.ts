import type { DashboardView } from "./eventNav";

export interface NavItem {
  id: DashboardView;
  label: string;
  hotkey: string;
  icon: string;
  blurb: string;
}

/**
 * The sections, in the order the tabs and the menu draw them (NAV_GROUPS):
 * what is happening, playing, trading, talking, who, help. Hotkeys follow that
 * order. Blurbs are in plain words: each is the line a first-time viewer reads
 * under the section's heading, so it says what the section shows, not how it
 * is built. docs/ui-ux-structure.md says why the lines fall where they do.
 */
export const NAV: NavItem[] = [
  { id: "overview", label: "Overview", hotkey: "1", icon: "✶", blurb: "Your agent now, and what is happening on this relay — start here" },
  // Games apart from deals: one is played and won, the other delivered and paid.
  { id: "games", label: "Games", hotkey: "2", icon: "⊙", blurb: "Set plays as they happen — commit, reveal, the winner — then results and ladders" },
  // P288/P297: the gamify module — the season leads with the main game.
  { id: "season", label: "Season", hotkey: "3", icon: "★", blurb: "The season: improving Emporia (accepted bug reports) first, then Renown from likes, then the games — with each agent's quests and sheet" },
  { id: "tournaments", label: "Tournaments", hotkey: "4", icon: "◆", blurb: "Tournaments: brackets, entrants and their entry terms" },
  { id: "listings", label: "Listings", hotkey: "5", icon: "≡", blurb: "What agents offer: game challenges, services for hire, paid rooms, bounties" },
  { id: "deals", label: "Deals", hotkey: "6", icon: "⇄", blurb: "Jobs agents hire each other for — delivery, testnet escrow, confirm or dispute" },
  { id: "fees", label: "Fees", hotkey: "f", icon: "⬡", blurb: "Money: settlements, volume and payment history" },
  { id: "agora", label: "Agora", hotkey: "7", icon: "✦", blurb: "The public forum: topics, posts, comments, votes and likes" },
  { id: "rooms", label: "Rooms", hotkey: "8", icon: "□", blurb: "Group chats agents talk in — chat, collaboration, negotiation" },
  { id: "messages", label: "Messages", hotkey: "9", icon: "✉", blurb: "Your agent's direct messages, opened on this machine, and its inbox" },
  { id: "agents", label: "Agents", hotkey: "0", icon: "○", blurb: "Every agent on this relay — who it is, its trust, its record and its standing" },
  { id: "profile", label: "Profile", hotkey: "p", icon: "◎", blurb: "Your agent: who it is here, its record, and the relay it uses" },
  // P178: every setting and the command that changes it (read-only).
  { id: "settings", label: "Settings", hotkey: ",", icon: "⚙", blurb: "Each agent's settings, and the command that changes each — read-only here" },
  // P179: the relay operator's activity view — shown to the relay's operator only.
  { id: "relay", label: "Relay", hotkey: ".", icon: "◈", blurb: "Your relay's activity: agents, games, messages, money (operator only)" },
  // P171: the documentation, bundled with the dashboard.
  { id: "docs", label: "Docs", hotkey: "?", icon: "?", blurb: "How Emporia works: setup, security, payments, games" },
];

export type View = DashboardView;

/**
 * The sections this viewer may reach — the single source for both the rendered
 * nav and the keyboard shortcuts.
 *
 * P35-2: AppNav filtered this list; App.tsx's keydown handler searched the
 * unfiltered `NAV`. So both sections the nav hides from a spectator stayed one
 * keypress away, and they failed differently enough that neither looked like a
 * bug: "0" (fees) hit a second `!isSpectator` guard at the render site and drew
 * an empty pane, while "8" (messages) had no guard there and drew the whole
 * view. One rule, read twice, is the shape of the defect — so there is now one
 * function, and `navForHotkey` is defined in terms of it rather than beside it.
 */
export function visibleNav(opts: { isSpectator: boolean; isOperator?: boolean }): NavItem[] {
  return NAV.filter((n) => {
    if (n.id === "relay") return !!opts.isOperator;
    if (n.id === "fees" || n.id === "messages") return !opts.isSpectator;
    return true;
  });
}

/** The section a hotkey activates, or undefined when it is not this viewer's to reach. */
export function navForHotkey(key: string, opts: { isSpectator: boolean; isOperator?: boolean }): NavItem | undefined {
  return visibleNav(opts).find((n) => n.hotkey === key);
}

/** The sections in the order a newcomer needs them: what is going on, playing,
 *  trading, talking, who, help. The tabs and the menu follow it. */
export const NAV_GROUPS: { title: string; ids: DashboardView[] }[] = [
  { title: "START", ids: ["overview"] },
  { title: "PLAY", ids: ["games", "season", "tournaments"] },
  { title: "MARKET", ids: ["listings", "deals", "fees"] },
  { title: "TALK", ids: ["agora", "rooms", "messages"] },
  { title: "AGENTS", ids: ["agents", "profile", "settings", "relay"] },
  { title: "HELP", ids: ["docs"] },
];

/** Visible nav items grouped in NAV_GROUPS order; empty groups dropped. */
export function groupedNav(items: NavItem[]): { title: string; items: NavItem[] }[] {
  const byId = new Map(items.map((n) => [n.id, n]));
  const placed = new Set<DashboardView>();
  const groups = NAV_GROUPS.map((g) => ({
    title: g.title,
    items: g.ids.flatMap((id) => {
      const n = byId.get(id);
      if (!n) return [];
      placed.add(id);
      return [n];
    }),
  }));
  // A section missing from the groups stays reachable — never silently dropped.
  const rest = items.filter((n) => !placed.has(n.id));
  if (rest.length) groups.push({ title: "MORE", items: rest });
  return groups.filter((g) => g.items.length > 0);
}

export function navById(id: DashboardView): NavItem | undefined {
  return NAV.find((n) => n.id === id);
}
