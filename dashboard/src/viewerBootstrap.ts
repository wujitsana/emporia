/**
 * P126: choose who the dashboard views as, *before* the app's modules load —
 * `VIEWER_ID` (relayEnv.ts) is read once, at import time.
 *
 * Via the local console there is no build-time identity: the viewer is a
 * `?as=` link, the session's earlier choice, or else the first agent whose
 * keys are on this machine — the Hermes profile's own agent first, when the tab
 * is open in one (P227). Imports nothing from the app for that reason.
 */
const VIEWER_KEY = "emporia_viewer"; // relayEnv.VIEWER_KEY

/** P231: the Hermes profile the tab is open in, and whether it is an agent here. */
export interface HermesProfile {
  name: string;
  isAgent: boolean;
}

export async function ensureViewer(
  listAgents: () => Promise<string[]>,
  profile: () => Promise<string> = async () => "",
): Promise<HermesProfile | null> {
  const fromUrl = new URLSearchParams(window.location.search).get("as");
  let saved: string | null = null;
  try {
    saved = sessionStorage.getItem(VIEWER_KEY);
  } catch {
    return null; // no session storage: the app falls back to spectator mode
  }
  if (fromUrl) return null;
  try {
    const [agents, here] = await Promise.all([listAgents(), profile().catch(() => "")]);
    // P227: in a Hermes profile that is one of these agents, view as it — opening
    // the tab from beta's profile showed alpha, the first name alphabetically.
    // P231: and in a profile that is no agent, view as nobody: showing alpha's
    // messages under another profile's name was wrong, not a convenience.
    // Inside Hermes the tab is the profile's: no switcher, so one profile's tab
    // never opens another agent's DMs.
    if (here) {
      const isAgent = agents.includes(here);
      if (isAgent) sessionStorage.setItem(VIEWER_KEY, here);
      else sessionStorage.removeItem(VIEWER_KEY);
      return { name: here, isAgent };
    }
    if (!saved && agents.length) sessionStorage.setItem(VIEWER_KEY, [...agents].sort()[0]);
  } catch {
    /* console unreachable: the app shows the error itself */
  }
  return null;
}
