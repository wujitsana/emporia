/**
 * P256/P281: one compact "Relay log" line — the relay's signed log state and this agent's own check of
 * it (relayLog.ts). Read only: it fetches two GETs and starts no check.
 */
import { useInterval } from "../hooks";
import { api } from "../api";
import { VIEWER_ID } from "../relayEnv";
import { fetchLogCheck, RELAY_LOG_ABOUT, relayLogView, type LogCheckReport, type LogState } from "../relayLog";

export function RelayLogLine({ relay, check }: {
  relay: { on: boolean; state: LogState | null } | null;
  check: LogCheckReport | null;
}) {
  if (!relay) return null;
  const view = relayLogView(relay, check);
  return (
    <div className={`e-relay-log${view.on ? "" : " is-off"}`} title={RELAY_LOG_ABOUT} aria-label="Relay log">
      <span className="e-relay-log__name">Relay log</span>
      <span className="e-relay-log__facts">{view.facts.join(" · ")}</span>
      {view.check && (
        <span className={`e-relay-log__check is-${view.check.tone}`} role={view.check.tone === "warn" ? "alert" : undefined}>
          {view.check.tone === "warn" && <span aria-hidden>! </span>}
          {view.check.text}
        </span>
      )}
      {view.on && <span className="e-relay-log__about">{RELAY_LOG_ABOUT}</span>}
    </div>
  );
}

/** The line with its data: the relay's state every minute, the agent's kept check beside it. */
export function RelayLog() {
  // Wrapped, so an api without the route (an older relay, a test's stub) is a missing line, not a crash.
  const [relay] = useInterval(() => Promise.resolve().then(() => api.logState()), 60_000);
  const [check] = useInterval(() => Promise.resolve().then(() => fetchLogCheck(VIEWER_ID)), 60_000);
  return <RelayLogLine relay={relay ?? null} check={check ?? null} />;
}
