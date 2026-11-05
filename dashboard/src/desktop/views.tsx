/** What the Desktop entry registers: the page, the status chip, the settings page. */
import { host, Switch, useValue, type PluginContext } from "@hermes/plugin-sdk";
import { useState } from "react";
import App from "../App";
import type { Session } from "../api";
import { gameName } from "../attention";
import { EMPORIA_VERSION } from "../buildInfo";
import { goToHash } from "../dashboardRoute";
import { AttentionChip } from "./chip";
import { ProfileMount, ScopedRoot } from "./mount";
import { currentProfile, NOTIFY_KEY, openAt, PAGE_PATH } from "./routes";

/** `/emporia`: the whole dashboard, for the profile Desktop is on, remounted on a switch. */
export function EmporiaPage() {
  const profile = currentProfile(useValue(host.state.profile));
  return (
    <ScopedRoot>
      <ProfileMount key={profile} profile={profile} render={() => <App />} />
    </ScopedRoot>
  );
}

export function StatusChip({ ctx }: { ctx: PluginContext }) {
  const profile = currentProfile(useValue(host.state.profile));
  const onNewTurn = (s: Session) => {
    if (!ctx.storage.get<boolean>(NOTIFY_KEY, false)) return;
    const others = s.participants.filter((p) => p !== profile).join(", ") || "—";
    const hash = `#/games/${encodeURIComponent(s.session_id)}`;
    ctx.os.notify({
      title: "Emporia: your turn",
      body: `${gameName(s.module_type)} v ${others}`,
      activate: PAGE_PATH,
      onActivate: () => goToHash(hash),
    });
  };
  return <AttentionChip key={profile} profile={profile} open={openAt} onNewTurn={onNewTurn} />;
}

export function SettingsPage({ ctx }: { ctx: PluginContext }) {
  const [on, setOn] = useState(() => ctx.storage.get<boolean>(NOTIFY_KEY, false));
  const set = (v: boolean) => {
    ctx.storage.set(NOTIFY_KEY, v);
    setOn(v);
  };
  return (
    <div style={{ display: "grid", gap: 12, maxWidth: 560, color: "var(--ui-text-secondary)" }}>
      <label style={{ display: "flex", gap: 12, alignItems: "center", color: "var(--ui-text-primary, inherit)" }}>
        <Switch id="emporia-notify-turn" checked={on} onCheckedChange={set} aria-label="Notify me on my agent's turn" />
        <span>Notify me when it is my agent's turn</span>
      </label>
      <p style={{ margin: 0 }}>
        An OS notification when one of the agent's games starts waiting on its move. Off by default; Hermes
        sends it only while you are away from the app. The status-bar chip shows the same thing either way.
      </p>
      <p style={{ margin: 0 }}>
        The page only watches: every move is the agent's own, through its tools. Plugin version {EMPORIA_VERSION}.
      </p>
    </div>
  );
}
