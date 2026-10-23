/**
 * P126: choose who the dashboard views as, *before* the app's modules load —
 * `VIEWER_ID` (relayEnv.ts) is read once, at import time.
 *
 * Via the local console there is no build-time identity: the viewer is a
 * `?as=` link, the session's earlier choice, or else the first agent whose
 * keys are on this machine. Imports nothing from the app for that reason.
 */
const VIEWER_KEY = "emporia_viewer"; // relayEnv.VIEWER_KEY

export async function ensureViewer(listAgents: () => Promise<string[]>): Promise<void> {
  const fromUrl = new URLSearchParams(window.location.search).get("as");
  let saved: string | null = null;
  try {
    saved = sessionStorage.getItem(VIEWER_KEY);
  } catch {
    return; // no session storage: the app falls back to spectator mode
  }
  if (fromUrl || saved) return;
  try {
    const agents = await listAgents();
    if (agents.length) sessionStorage.setItem(VIEWER_KEY, [...agents].sort()[0]);
  } catch {
    /* console unreachable: the app shows the error itself */
  }
}
