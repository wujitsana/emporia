/** Which agents this machine's console has keys for, or why it cannot say (desktop/mount.tsx). */
import { consoleUrl, httpFetch } from "../transport";

export type Probe = { agents: string[] } | { setup: true } | { error: string };

export async function probeConsole(): Promise<Probe> {
  try {
    const r = await httpFetch(consoleUrl("/agents"));
    if (r.status === 503) {
      const d = (await r.json().catch(() => ({}))) as { setup_needed?: boolean; detail?: string };
      // P296: no Emporia settings in this profile — the page shows `hermes emporia setup`.
      return d.setup_needed ? { setup: true } : { error: d.detail ?? "the Emporia console is not answering" };
    }
    if (!r.ok) return { error: `the plugin answered ${r.status}` };
    const d = (await r.json()) as { agents?: unknown };
    return { agents: Array.isArray(d.agents) ? d.agents.filter((a): a is string => typeof a === "string") : [] };
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}

let lastProfile: string | null = null;

/** Whether `profile` is a switch from the one shown before (not the first open). Records it. */
export function isProfileSwitch(profile: string): boolean {
  const switched = lastProfile !== null && lastProfile !== profile;
  lastProfile = profile;
  return switched;
}
