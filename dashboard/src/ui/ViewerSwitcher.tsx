import React from "react";
import { api } from "../api";
import { useInterval } from "../hooks";
import { useRelayCtx } from "../relayCtx";
import { VIEWER_ID, VIEWER_KEY } from "../relayEnv";
import { VIA_CONSOLE, consoleUrl, hermesProfile, httpFetch } from "../transport";

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
  const hermes = hermesProfile();
  return hermes ? <HermesProfileSelect current={hermes} /> : <ViewerSelect />;
}

/**
 * P234: inside the Hermes dashboard the tab belongs to a Hermes profile, so
 * picking an agent switches *Hermes's* profile — its own picker follows (it
 * reads ?profile= from the URL) and the tab reloads as that agent. Only
 * agents registered on the relay, with keys here and a Hermes profile.
 */
function HermesProfileSelect({ current }: { current: { name: string; isAgent: boolean } }) {
  const [ids, setIds] = React.useState<string[]>([]);
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      const local: string[] = await httpFetch(consoleUrl("/agents")).then((r) => r.json())
        .then((d) => d.agents ?? []).catch(() => []);
      const profiles: string[] = await httpFetch("/api/profiles").then((r) => r.json())
        .then((d) => (d.profiles ?? []).map((p: { name: string }) => p.name)).catch(() => []);
      const asker = local.find((a) => profiles.includes(a));
      const registered: string[] = asker
        ? await httpFetch(`${consoleUrl(`/relay/${encodeURIComponent(asker)}`)}/agents`).then((r) => r.json())
          .then((d) => (d.agents ?? []).map((a: { agent_id: string }) => a.agent_id)).catch(() => [])
        : [];
      if (!cancelled) setIds(local.filter((a) => profiles.includes(a) && registered.includes(a)).sort());
    })();
    return () => { cancelled = true; };
  }, []);

  const choose = (e: React.ChangeEvent<HTMLSelectElement>) => {
    if (!e.target.value) return;
    const url = new URL(window.location.href);
    url.searchParams.set("profile", e.target.value);
    window.history.pushState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  };

  return (
    <label className="e-viewer-switch" title="Switch the Hermes profile to this agent's">
      <span className="e-faint">as</span>
      <select value={current.isAgent ? current.name : ""} onChange={choose} aria-label="Agent">
        {!current.isAgent && <option value="">choose an agent</option>}
        {ids.map((id) => <option key={id} value={id}>{id}</option>)}
      </select>
    </label>
  );
}

function ViewerSelect() {
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
