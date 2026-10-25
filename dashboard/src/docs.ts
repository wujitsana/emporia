/**
 * P171: the documents the dashboard carries, in reading order.
 *
 * Bundled when the dashboard is built (Vite `?raw` imports), so the docs always
 * match the code they describe, work offline, and need no server route: nothing
 * reads files at run time. The long history files (the security log, the
 * archive) stay in the repository.
 */
import overview from "../../README.md?raw";
import useCases from "../../docs/use-cases.md?raw";
import games from "../../docs/games.md?raw";
import agentSetup from "../../docs/agent-security-setup.md?raw";
import payments from "../../docs/payments.md?raw";
import addingAGame from "../../docs/adding-a-game.md?raw";
import rogueRelay from "../../docs/if-a-relay-goes-rogue.md?raw";
import localConsole from "../../docs/local-console.md?raw";
import runbook from "../../docs/RUNBOOK.md?raw";
import security from "../../SECURITY.md?raw";
import roadmap from "../../ROADMAP.md?raw";
import manualTesting from "../../docs/manual-testing.md?raw";
import type { LinkTarget } from "./markdown";

export interface Doc {
  id: string;
  title: string;
  blurb: string;
  /** Path in the repository, for "where is this" and for resolving links. */
  path: string;
  text: string;
}

export const DOCS: Doc[] = [
  { id: "use-cases", title: "Suggested use cases", path: "docs/use-cases.md", text: useCases,
    blurb: "What Emporia is good for, and how each is set up" },
  { id: "agent-setup", title: "Setting up agents safely", path: "docs/agent-security-setup.md", text: agentSetup,
    blurb: "Roles, where keys live, wallets, limits, Stripe, sandboxes" },
  { id: "games", title: "Gaming", path: "docs/games.md", text: games,
    blurb: "The games, playing, staking, fair play, checking results" },
  { id: "overview", title: "Overview", path: "README.md", text: overview,
    blurb: "What Emporia is, install, every setting" },
  { id: "payments", title: "Payments", path: "docs/payments.md", text: payments,
    blurb: "Cards, USDC, escrow, Tempo — who pays whom, and fees" },
  { id: "security", title: "Security", path: "SECURITY.md", text: security,
    blurb: "What a relay can and cannot do; open questions" },
  { id: "rogue-relay", title: "If a relay goes rogue", path: "docs/if-a-relay-goes-rogue.md", text: rogueRelay,
    blurb: "Evidence, verifying games, taking money back" },
  { id: "adding-a-game", title: "Adding a game", path: "docs/adding-a-game.md", text: addingAGame,
    blurb: "Write a game module" },
  { id: "local-console", title: "The local dashboard", path: "docs/local-console.md", text: localConsole,
    blurb: "Where this dashboard runs and why" },
  { id: "runbook", title: "Running a relay", path: "docs/RUNBOOK.md", text: runbook,
    blurb: "Operating, restarting, troubleshooting" },
  { id: "roadmap", title: "Roadmap", path: "ROADMAP.md", text: roadmap,
    blurb: "What is left to build, and why" },
  { id: "manual-testing", title: "Manual test plan", path: "docs/manual-testing.md", text: manualTesting,
    blurb: "Checking everything by hand" },
];

const BY_PATH = new Map(DOCS.map((d) => [d.path, d]));

/** Normalise `../docs/x.md`, `./x.md`, `x.md` against the linking doc's folder. */
function joinPath(fromPath: string, href: string): string {
  const parts = fromPath.split("/").slice(0, -1);
  for (const seg of href.split("/")) {
    if (seg === "..") parts.pop();
    else if (seg && seg !== ".") parts.push(seg);
  }
  return parts.join("/");
}

/** What a link inside `from` points at. */
export function resolveDocLink(from: Doc, href: string): LinkTarget {
  if (/^https?:\/\//i.test(href)) return { kind: "web", href };
  if (href.startsWith("#")) return { kind: "anchor", anchor: href.slice(1) };
  if (/^[a-z]+:/i.test(href)) return { kind: "none" };          // mailto:, file:, …
  const [pathPart, anchor] = href.split("#");
  const doc = BY_PATH.get(joinPath(from.path, pathPart));
  return doc ? { kind: "doc", id: doc.id, anchor } : { kind: "none" };
}

/** Docs whose title, blurb or text mention every word of `query`. */
export function searchDocs(query: string): Doc[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return DOCS;
  return DOCS.filter((d) => {
    const hay = `${d.title} ${d.blurb} ${d.text}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}
