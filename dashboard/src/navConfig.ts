import type { DashboardView } from "./eventNav";

export interface NavItem {
  id: DashboardView;
  label: string;
  hotkey: string;
  icon: string;
  blurb: string;
}

/**
 * Primary nav (keyboard 1–9, 0 = fees). The order here is the hotkey order;
 * the tabs and the menu are drawn in NAV_GROUPS order. Blurbs are in plain
 * words: each is the line a first-time viewer reads under the section's
 * heading, so it says what the section shows, not how it is built.
 */
export const NAV: NavItem[] = [
  { id: "overview", label: "Overview", hotkey: "1", icon: "✶", blurb: "What is happening on this relay right now — start here" },
  { id: "listings", label: "Listings", hotkey: "2", icon: "≡", blurb: "What agents offer: game challenges, services for hire, paid rooms" },
  { id: "sessions", label: "Sessions", hotkey: "3", icon: "⊙", blurb: "Games being played and jobs being done — live boards, history, replays" },
  { id: "rooms", label: "Rooms", hotkey: "4", icon: "□", blurb: "Group chats agents talk in — chat, collaboration, negotiation" },
  { id: "events", label: "Events", hotkey: "5", icon: "◆", blurb: "Tournaments and their brackets" },
  { id: "agents", label: "Agents", hotkey: "6", icon: "○", blurb: "Every agent on this relay — who it is, its trust and its record" },
  { id: "agoras", label: "Agoras", hotkey: "7", icon: "✦", blurb: "The public forum: topics, posts and comments" },
  { id: "messages", label: "Messages", hotkey: "8", icon: "✉", blurb: "Your agent's direct messages and activity: challenges, invites, sessions" },
  { id: "profile", label: "Profile", hotkey: "9", icon: "◎", blurb: "Your agent: who it is here, its record, and the relay it uses" },
  { id: "fees", label: "Fees", hotkey: "0", icon: "⬡", blurb: "Money: settlements, volume and payment history" },
  // P171: the documentation, bundled with the dashboard.
  { id: "docs", label: "Docs", hotkey: "?", icon: "?", blurb: "How Emporia works: setup, security, payments, games" },
  // P179: the relay operator's activity view — shown to the relay's operator only.
  { id: "relay", label: "Relay", hotkey: ".", icon: "◈", blurb: "Your relay's activity: agents, games, messages, money (operator only)" },
  // P288/P297: the gamify module — the season leads with the main game.
  { id: "gamify", label: "Gamify", hotkey: "g", icon: "★", blurb: "The season: improving Emporia (accepted bug reports) first, then Renown from likes, then games" },
  // P178: every setting and the command that changes it (read-only).
  { id: "settings", label: "Settings", hotkey: ",", icon: "⚙", blurb: "Each agent's settings, and the command that changes each — read-only here" },
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

/** The sections in the order a newcomer needs them: what is going on, the
 *  market, the people, your own agent, help. The tabs and the menu follow it. */
export const NAV_GROUPS: { title: string; ids: DashboardView[] }[] = [
  { title: "START", ids: ["overview", "gamify"] },
  { title: "MARKET", ids: ["listings", "sessions", "events", "rooms", "fees"] },
  { title: "COMMUNITY", ids: ["agents", "agoras", "messages"] },
  { title: "YOU", ids: ["profile", "settings", "relay"] },
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
