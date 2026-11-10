import { useState } from "react";
import { api } from "../api";
import { poll } from "../capture";
import { useInterval } from "../hooks";
import { nowLine } from "../nowLine";
import { readPref, writePref } from "../prefs";
import { VIEWER_ID } from "../relayEnv";
import type { Navigate } from "../navigation";

const PREF = "emporia_now_strip_collapsed";

/**
 * One read-only line under the sub-row, on every view but Home (Home has the whole block): what the
 * viewed agent is doing now. Click opens the game. It can be folded away; the choice is kept on this machine.
 */
export function NowStrip({ navigate }: { navigate: Navigate }) {
  const viewer = VIEWER_ID;
  const [collapsed, setCollapsed] = useState(() => readPref(PREF) === "1");
  const [sessions] = useInterval(
    () => (viewer ? api.agentSessions(viewer).then((r) => r.sessions) : Promise.resolve([])),
    poll(10_000), [viewer],
  );
  if (!viewer) return null;
  const line = nowLine(viewer, sessions ?? []);
  const toggle = () => { const next = !collapsed; setCollapsed(next); writePref(PREF, next ? "1" : "0"); };
  return (
    <div className={`e-now-strip${collapsed ? " is-collapsed" : ""}`} role="status">
      {collapsed ? null : (
        <button
          type="button"
          className="e-now-strip__main"
          disabled={!line.session}
          onClick={() => line.session && navigate("session", { sessionId: line.session.session_id, gameModuleType: line.session.module_type })}
        >
          {line.live ? <span className="e-now-strip__live" aria-hidden>●</span> : null}
          <span>{line.text}</span>
        </button>
      )}
      <button type="button" className="e-now-strip__toggle" onClick={toggle} aria-expanded={!collapsed}
        title={collapsed ? "Show what the agent is doing" : "Hide this line"}>
        {collapsed ? "now ▾" : "▴"}
      </button>
    </div>
  );
}
