/**
 * P171: the docs' texts, bundled at build time (Vite `?raw`) — the console and
 * relay builds. The Hermes tab's build swaps this file for docTexts.plugin.ts
 * (vite.plugin.config.ts): its bundle carries no doc text, and the console
 * serves the texts from the Emporia it runs, the same commit as the code.
 */
import createAnAgent from "../../docs/create-an-agent.md?raw";
import useCases from "../../docs/use-cases.md?raw";
import anyAgent from "../../docs/any-agent.md?raw";
import agentSetup from "../../docs/agent-security-setup.md?raw";
import games from "../../docs/games.md?raw";
import overview from "../../README.md?raw";
import payments from "../../docs/payments.md?raw";
import security from "../../SECURITY.md?raw";
import rogueRelay from "../../docs/if-a-relay-goes-rogue.md?raw";
import localConsole from "../../docs/local-console.md?raw";

/** Every doc's text by its path, or null when they are fetched (the tab). */
export const BUNDLED_TEXTS: Record<string, string> | null = {
  "docs/create-an-agent.md": createAnAgent,
  "docs/use-cases.md": useCases,
  "docs/any-agent.md": anyAgent,
  "docs/agent-security-setup.md": agentSetup,
  "docs/games.md": games,
  "README.md": overview,
  "docs/payments.md": payments,
  "SECURITY.md": security,
  "docs/if-a-relay-goes-rogue.md": rogueRelay,
  "docs/local-console.md": localConsole,
};

export async function fetchDocTexts(): Promise<Record<string, string>> {
  return BUNDLED_TEXTS ?? {};
}
