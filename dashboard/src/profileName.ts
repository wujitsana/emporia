import { useEffect, useState } from "react";
import { VIEWER_ID } from "./relayEnv";
import { consoleUrl, hermesProfile, httpFetch, MODE, VIA_CONSOLE } from "./transport";

/** The NAME in a `hermes emporia … NAME …` command: the viewer's Hermes profile — the tab's
 *  own, or the console's record of which profile holds the viewer's keys (GET /agents) —
 *  else its agent id, else the placeholder NAME. */
export function useProfileName(viewer: string | null = VIEWER_ID): string {
  const tab = MODE === "hermes" ? hermesProfile()?.name : undefined;
  const [fromConsole, setFromConsole] = useState<string | null>(null);
  useEffect(() => {
    if (tab || !VIA_CONSOLE || !viewer) return;
    let live = true;
    httpFetch(consoleUrl("/agents"))
      .then((r) => (r.ok ? r.json() : null))
      .then((d: { profiles?: Record<string, string> } | null) => {
        if (live) setFromConsole(d?.profiles?.[viewer] ?? null);
      })
      .catch(() => undefined);
    return () => { live = false; };
  }, [tab, viewer]);
  return tab || fromConsole || viewer || "NAME";
}
