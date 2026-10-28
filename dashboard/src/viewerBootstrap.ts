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

export async function ensureViewer(
  listAgents: () => Promise<string[]>,
  profile: () => Promise<string> = async () => "",
): Promise<void> {
  const fromUrl = new URLSearchParams(window.location.search).get("as");
  let saved: string | null = null;
  try {
    saved = sessionStorage.getItem(VIEWER_KEY);
  } catch {
    return; // no session storage: the app falls back to spectator mode
  }
  if (fromUrl) return;
  try {
    const [agents, here] = await Promise.all([listAgents(), profile().catch(() => "")]);
    // P227: in a Hermes profile that is one of these agents, view as it — opening
    // the tab from beta's profile showed alpha, the first name alphabetically.
    if (here && agents.includes(here)) sessionStorage.setItem(VIEWER_KEY, here);
    else if (!saved && agents.length) sessionStorage.setItem(VIEWER_KEY, [...agents].sort()[0]);
  } catch {
    /* console unreachable: the app shows the error itself */
  }
}
