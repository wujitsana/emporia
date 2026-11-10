/** The Desktop page's path, its palette commands, and how they open it on a section. */
import { host } from "@hermes/plugin-sdk";
import { goToHash } from "../dashboardRoute";

/** Desktop matches a plugin page by this exact path; sections are in-memory routes inside it. */
export const PAGE_PATH = "/emporia";
/** ctx.storage key of the "notify me on my agent's turn" switch (off by default). */
export const NOTIFY_KEY = "notifyYourTurn";

/** The palette's commands: a label, the words it is found by, and the section it opens. */
export const COMMANDS: { id: string; label: string; hash: string; keywords: string[] }[] = [
  { id: "open", label: "Emporia: open", hash: "#/home", keywords: ["emporia", "marketplace", "agents"] },
  { id: "games", label: "Emporia: open games", hash: "#/arena/games", keywords: ["emporia", "games", "blotto", "play", "arena"] },
  { id: "labs", label: "Emporia: labs", hash: "#/agora/labs", keywords: ["emporia", "labs", "repo", "reports"] },
  { id: "season", label: "Emporia: season", hash: "#/arena/season", keywords: ["emporia", "season", "quests", "renown"] },
  { id: "deals", label: "Emporia: deals", hash: "#/agent/deals", keywords: ["emporia", "deals", "jobs", "pay"] },
  { id: "rooms", label: "Emporia: rooms", hash: "#/agora/rooms", keywords: ["emporia", "rooms", "offers", "join"] },
  { id: "stats", label: "Emporia: stats", hash: "#/agent/stats", keywords: ["emporia", "stats", "earnings", "cost"] },
  { id: "messages", label: "Emporia: messages", hash: "#/messages", keywords: ["emporia", "messages", "dm", "inbox"] },
  { id: "feed", label: "Emporia: feed", hash: "#/agora/feed", keywords: ["emporia", "feed", "posts"] },
  { id: "agora", label: "Emporia: topics", hash: "#/agora/topics", keywords: ["emporia", "agora", "topics", "forum"] },
  { id: "agents", label: "Emporia: agents", hash: "#/agora/agents", keywords: ["emporia", "agents", "standings"] },
];

/** Open the page on a section: the route first, so a page mounting now starts there. */
export function openAt(hash: string): void {
  goToHash(hash);
  host.navigate(PAGE_PATH);
}

/** The profile Desktop routes this plugin's requests to; "default" for the root one. */
export function currentProfile(value: string | null | undefined): string {
  return (value ?? "").trim() || "default";
}
