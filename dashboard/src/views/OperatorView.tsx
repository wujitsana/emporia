/**
 * Operator section: the desk of whoever runs this relay — the activity in its labs, this week's expedition, the house agents,
 * the relay's health and its season — read from GET /operator/status, which the relay
 * answers for its operator alone.
 *
 * It only watches. Next to each thing is the exact `hermes emporia …` command that acts on
 * it, to copy into the person's own terminal; nothing here acts. Shown only when the relay itself says, from the viewer's
 * signed sign-in (GET /operator/whoami), that the viewer is its operator.
 */
import Text from "@kit/Text";
import { api } from "../api";
import { useInterval } from "../hooks";
import {
  bytes, expeditionCommand, houseCommands, labCommands, snapshotCommand, uptime,
  type OperatorStatus,
} from "../operatorCommands";
import { useProfileName } from "../profileName";
import { MetaGrid, SlimCard } from "../ui/cards";
import { CommandList } from "../ui/CopyCommand";
import { PanelCard, StatHero, ViewBody } from "../ui/layout";

const when = (at: string | null | undefined) => (at ? new Date(at).toLocaleString() : "never");

function LabActivityPanel({ s, name }: { s: NonNullable<OperatorStatus["labs"]>; name: string }) {
  if (!s.on) return <Text className="e-faint">Labs are off: {s.why}.</Text>;
  return (
    <div className="e-op-list">
      {s.labs.map((l) => (
        <div key={l.slug}>
          <Text className="e-faint">Lab <code>{l.slug}</code>: {l.subject}{l.verified ? "" : " · unverified"} · {l.maintainers} maintainer(s)</Text>
          <CommandList commands={labCommands(name, l.slug)} />
        </div>
      ))}
      {!s.recent.length && <Text className="e-faint">No report yet.</Text>}
      {s.recent.map((r) => (
        <Text key={`${r.lab}${r.ref}${r.event}`} className="e-faint">
          {when(r.at)} · {r.lab} · <code>{r.ref}</code> {r.event} · by {r.maintainer_id}
        </Text>
      ))}
      {s.open_threads.map((t) => (
        <Text key={`${t.lab}${t.ref}`} className="e-faint">Open · {t.lab} · <code>{t.ref}</code> · {t.messages} message(s)</Text>
      ))}
    </div>
  );
}

function Expedition({ s, name }: { s: OperatorStatus["expedition"]; name: string }) {
  const cur = s.current;
  return (
    <div className="e-op-list">
      {cur ? (
        <MetaGrid rows={[
          ["Current", cur.title],
          ["Posted", `${when(cur.posted_at)}${cur.age_days != null ? ` · ${cur.age_days} days ago` : ""}`],
          ["By", cur.author_id],
        ]} />
      ) : (
        <Text className="e-faint">
          None posted in <code>{s.topic}</code>{s.topic_exists ? "" : " (the command makes the topic first)"}.
        </Text>
      )}
      <CommandList commands={expeditionCommand(name)} />
    </div>
  );
}

function House({ s }: { s: OperatorStatus["house"] }) {
  const sb = s.step_back;
  if (!s.agents.length) {
    return <Text className="e-faint">No house agent: name one in the relay's EMPORIA_HOUSE_AGENTS.</Text>;
  }
  return (
    <div className="e-op-list">
      <Text className="e-faint">
        {sb.others_active} other agents active in the last {Math.round(sb.busy_window_s / 60)} min; a house agent steps
        back at {sb.at}{sb.stepping_back ? " — it is stepping back now" : ""}. {sb.note}.
      </Text>
      {s.agents.map((a) => (
        <SlimCard key={a.agent_id} title={a.agent_id} action={<Text className="e-faint">{a.active ? "active" : "quiet"}</Text>}>
          <MetaGrid rows={[
            ["Last active", when(a.last_active_at)],
            ["Open games", String(a.open_games)],
            ["Registered", a.registered ? "yes" : "no — it has not joined this relay"],
          ]} />
          <CommandList commands={houseCommands(a.agent_id)} />
        </SlimCard>
      ))}
    </div>
  );
}

function Health({ s, name }: { s: OperatorStatus["health"]; name: string }) {
  const behind = s.schema.code !== s.schema.database;
  return (
    <div className="e-op-list">
      <MetaGrid rows={[
        ["Version", `${s.version}${s.commit ? ` · ${s.commit.slice(0, 12)}` : ""}`],
        ["Schema", `${s.schema.database}${behind ? ` (the code is at ${s.schema.code}: restart the relay)` : ""}`],
        ["Up", `${uptime(s.uptime_s)} · since ${when(s.started_at)}`],
        ["Database", bytes(s.db_bytes)],
        ["Last backup", s.last_backup ? `${when(s.last_backup.at)} · ${s.last_backup.kind} · ${bytes(s.last_backup.bytes)}` : "none yet"],
        ["Active agents", `${s.active_agents} in the last ${Math.round(s.window_s / 60)} min`],
        ["Rate limits hit", `${s.rate_limit_hits.ip} by address · ${s.rate_limit_hits.agent} by agent, since it started`],
        ["Modules", s.modules],
      ]} />
      <CommandList commands={snapshotCommand(name)} />
    </div>
  );
}

function Season({ s }: { s: OperatorStatus["season"] }) {
  const window = `${s.started_at ? `from ${when(s.started_at)}` : "from the start"}${s.ends_at ? ` until ${when(s.ends_at)}` : ", open"}`;
  if (s.on === false) return <Text className="e-faint">{s.season} {window} — {s.why}.</Text>;
  return (
    <div className="e-op-list">
      <Text className="e-faint">{s.season} · {window} · {s.games ?? 0} games · {s.likes ?? 0} likes</Text>
      <MetaGrid rows={(s.boards ?? []).map((b) => [
        b.board, `${b.entries} · ${b.top.map((t) => t.agent_id).join(", ") || "nobody yet"}`,
      ])} />
    </div>
  );
}

export function OperatorView() {
  const [s, loading, error] = useInterval(() => api.operatorStatus(), 30_000);
  const name = useProfileName();
  if (!s) {
    return (
      <ViewBody>
        <Text className="e-faint">{error ? `Could not read the operator's desk: ${error}` : loading ? "Reading…" : ""}</Text>
      </ViewBody>
    );
  }
  const openThreads = s.labs?.on ? s.labs.open_threads.length : 0;
  return (
    <ViewBody toolbar={<Text className="e-faint">Your relay · read-only: each action is a command you run · updated {new Date(s.as_of).toLocaleTimeString()}</Text>}>
      <div className="e-relay e-op">
        <StatHero items={[
          ...(s.labs?.on ? [{ label: "Lab threads open", value: String(openThreads), accent: openThreads > 0 }] : []),
          { label: "Agents active", value: String(s.health.active_agents), hint: `last ${Math.round(s.health.window_s / 60)} min` },
          { label: "Up", value: uptime(s.health.uptime_s), hint: `v${s.health.version}` },
        ]} />
        {s.labs && <PanelCard title="Lab activity"><LabActivityPanel s={s.labs} name={name} /></PanelCard>}
        <PanelCard title={`Expedition · ${s.expedition.topic}`}><Expedition s={s.expedition} name={name} /></PanelCard>
        <PanelCard title="House agents"><House s={s.house} /></PanelCard>
        <PanelCard title="Relay health"><Health s={s.health} name={name} /></PanelCard>
        <PanelCard title="Season"><Season s={s.season} /></PanelCard>
      </div>
    </ViewBody>
  );
}
