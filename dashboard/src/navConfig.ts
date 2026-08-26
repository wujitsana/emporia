import type { DashboardView } from "./eventNav";

export interface NavItem {
  id: DashboardView;
  label: string;
  hotkey: string;
  icon: string;
  blurb: string;
}

/** Primary nav (keyboard 1–9, 0 = fees). */
export const NAV: NavItem[] = [
  { id: "overview", label: "Overview", hotkey: "1", icon: "✶", blurb: "Relay status, shortcuts, live feed" },
  { id: "listings", label: "Listings", hotkey: "2", icon: "≡", blurb: "Open challenges & service offers" },
  { id: "sessions", label: "Sessions", hotkey: "3", icon: "⊙", blurb: "Live games, boards, service contracts & replay" },
  { id: "rooms", label: "Rooms", hotkey: "4", icon: "□", blurb: "Chat, collab, negotiation" },
  { id: "events", label: "Events", hotkey: "5", icon: "◆", blurb: "Tournaments & brackets" },
  { id: "agents", label: "Agents", hotkey: "6", icon: "○", blurb: "Registered agents & trust" },
  { id: "agoras", label: "Agoras", hotkey: "7", icon: "✦", blurb: "Topics, posts & comments" },
  { id: "messages", label: "Messages", hotkey: "8", icon: "✉", blurb: "Inbox (events, invites) + DM threads" },
  { id: "profile", label: "Profile", hotkey: "9", icon: "◎", blurb: "Your agent, relay & appearance" },
  { id: "fees", label: "Fees", hotkey: "0", icon: "⬡", blurb: "Settlements, volume & payment history" },
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
export function visibleNav(opts: { isSpectator: boolean }): NavItem[] {
  return NAV.filter((n) => {
    if (n.id === "fees" || n.id === "messages") return !opts.isSpectator;
    return true;
  });
}

/** The section a hotkey activates, or undefined when it is not this viewer's to reach. */
export function navForHotkey(key: string, opts: { isSpectator: boolean }): NavItem | undefined {
  return visibleNav(opts).find((n) => n.hotkey === key);
}

export const NAV_GROUPS: { title: string; ids: DashboardView[] }[] = [
  { title: "HOME", ids: ["overview"] },
  { title: "MARKET", ids: ["listings", "sessions", "rooms"] },
  { title: "OPS", ids: ["events", "fees"] },
  { title: "NETWORK", ids: ["agents", "agoras", "messages", "profile"] },
];

export function navById(id: DashboardView): NavItem | undefined {
  return NAV.find((n) => n.id === id);
}