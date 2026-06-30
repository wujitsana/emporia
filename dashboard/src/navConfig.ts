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

export const NAV_GROUPS: { title: string; ids: DashboardView[] }[] = [
  { title: "HOME", ids: ["overview"] },
  { title: "MARKET", ids: ["listings", "sessions", "rooms"] },
  { title: "OPS", ids: ["events", "fees"] },
  { title: "NETWORK", ids: ["agents", "agoras", "messages", "profile"] },
];

export function navById(id: DashboardView): NavItem | undefined {
  return NAV.find((n) => n.id === id);
}