import React from "react";
import { api } from "../api";
import { useInterval } from "../hooks";
import { useRelayCtx } from "../relayCtx";
import { VIEWER_ID } from "../relayEnv";

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
  const [data] = useInterval(
    () => (headerAuthTrusted ? api.agents() : Promise.resolve({ agents: [], count: 0 })),
    60_000,
    [headerAuthTrusted],
  );
  if (!headerAuthTrusted) return null;
  const ids = (data?.agents ?? []).map((a) => a.agent_id).sort();
  if (VIEWER_ID && !ids.includes(VIEWER_ID)) ids.unshift(VIEWER_ID);

  const choose = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const url = new URL(window.location.href);
    url.searchParams.set("as", e.target.value);
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
