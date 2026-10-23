import React from "react";
import { api } from "../api";
import { useInterval } from "../hooks";
import { useRelayCtx } from "../relayCtx";
import { VIEWER_ID, VIEWER_KEY } from "../relayEnv";
import { VIA_CONSOLE, consoleUrl, httpFetch } from "../transport";

/**
 * P123: view the dashboard as another agent on this relay — alpha, beta, the
 * operator — without rebuilding it. Picking one reloads with `?as=<agent>`.
 *
 * Shown only when the relay trusts this machine's header sign-in
 * (EMPORIA_TRUST_LOCAL_HEADER_AUTH): the local, single-operator case, where
 * every agent on the relay is the operator's own. A dashboard pointed at
 * someone else's relay never sees it, and switching grants nothing — the relay
 * still decides what each identity may see.
 */
export function ViewerSwitcher() {
  const { headerAuthTrusted } = useRelayCtx();
  // P126: via the local console the choices are the agents whose keys are on
  // this machine — the only ones it can sign in as — and always shown.
  const show = VIA_CONSOLE || headerAuthTrusted;
  const [data] = useInterval(
    () => !show ? Promise.resolve({ ids: [] as string[] })
      : VIA_CONSOLE
        ? httpFetch(consoleUrl("/agents")).then((r) => r.json()).then((d) => ({ ids: d.agents as string[] }))
        : api.agents().then((d) => ({ ids: d.agents.map((a) => a.agent_id) })),
    60_000,
    [show],
  );
  if (!show) return null;
  const ids = [...(data?.ids ?? [])].sort();
  if (VIEWER_ID && !ids.includes(VIEWER_ID)) ids.unshift(VIEWER_ID);

  const choose = (e: React.ChangeEvent<HTMLSelectElement>) => {
    try { sessionStorage.setItem(VIEWER_KEY, e.target.value); } catch { /* private mode */ }
    const url = new URL(window.location.href);
    if (VIA_CONSOLE) url.searchParams.delete("as");   // the session choice decides
    else url.searchParams.set("as", e.target.value);
    window.location.assign(url.toString());
  };

  return (
    <label className="e-viewer-switch" title="View this dashboard as another agent on this relay (local only)">
      <span className="e-faint">as</span>
      <select value={VIEWER_ID ?? ""} onChange={choose} aria-label="View as agent">
        {!VIEWER_ID && <option value="">spectator</option>}
        {ids.map((id) => <option key={id} value={id}>{id}</option>)}
      </select>
    </label>
  );
}
