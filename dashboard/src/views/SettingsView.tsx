/**
 * P178: every setting `emporia` changes — each agent's and this machine's —
 * with its current value and the command that changes it. Read-only: the
 * dashboard watches, a person changes things in their own terminal. Read from
 * the local console (GET /settings), which holds no secret in its answer.
 */
import { useMemo, useState } from "react";
import Text from "@components/Text";
import SidebarLayout from "@components/SidebarLayout";
import { FlatRailItem } from "../ui/FlatRailItem";
import { ViewBody } from "../ui/layout";
import { useInterval } from "../hooks";
import { consoleUrl, httpFetch, VIA_CONSOLE } from "../transport";
import { VIEWER_ID } from "../relayEnv";
import { groupItems, type SettingItem, type SettingsReport } from "../settings";


const MACHINE = "__machine__";

async function fetchSettings(): Promise<SettingsReport> {
  const r = await httpFetch(consoleUrl("/settings"));
  if (!r.ok) throw new Error(`settings ${r.status}`);
  return r.json();
}


function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(command).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }, () => undefined);
  };
  return (
    <span className="e-setting-cmd">
      <code>{command}</code>
      <button type="button" className="e-setting-copy" onClick={copy} aria-label={`Copy: ${command}`}>
        {copied ? "copied" : "copy"}
      </button>
    </span>
  );
}

function SettingRow({ item }: { item: SettingItem }) {
  const mark = item.level === "danger" ? "✗" : item.level === "warn" ? "!" : "✓";
  return (
    <div className={`e-setting e-setting-${item.level}`}>
      <div className="e-setting-head">
        <span className="e-setting-mark" aria-hidden>{mark}</span>
        <Text className="e-setting-label">{item.label}</Text>
        {item.value && <Text className="e-setting-value">{item.value}</Text>}
      </div>
      <Text className="e-faint e-setting-about">{item.about}</Text>
      {item.command && <CopyCommand command={item.command} />}
      {item.person_only && (
        <Text className="e-faint e-setting-note">
          Only a person runs this, in their own terminal — never through an agent.
        </Text>
      )}
    </div>
  );
}

export function SettingsView() {
  const [report, loading, error] = useInterval(fetchSettings, 60_000);
  const [picked, setPicked] = useState<string | null>(null);
  const agents = useMemo(() => report?.agents ?? [], [report]);
  const selected = picked ?? (agents.find((a) => a.agent_id === VIEWER_ID)?.agent_id ?? agents[0]?.agent_id ?? MACHINE);
  const items = useMemo(
    () => (selected === MACHINE ? report?.machine : agents.find((a) => a.agent_id === selected)?.items) ?? [],
    [report, agents, selected]);
  const warnings = (list: SettingItem[]) => list.filter((i) => i.level !== "ok").length;

  if (!VIA_CONSOLE) {
    return (
      <ViewBody>
        <Text className="e-faint">
          Settings are read on the machine that holds the agents' keys. Open this dashboard from the
          Hermes dashboard's Emporia tab, or with <code>python -m emporia.console</code>.
        </Text>
      </ViewBody>
    );
  }

  const sidebar = (
    <div className="e-docs-rail">
      {agents.map((a) => (
        <FlatRailItem key={a.agent_id} selected={a.agent_id === selected} onClick={() => setPicked(a.agent_id)}>
          <div className="e-docs-rail-item">
            <Text>{a.agent_id}</Text>
            <Text className="e-faint">
              {a.profile}{warnings(a.items) ? ` · ${warnings(a.items)} to look at` : ""}
            </Text>
          </div>
        </FlatRailItem>
      ))}
      <FlatRailItem selected={selected === MACHINE} onClick={() => setPicked(MACHINE)}>
        <div className="e-docs-rail-item">
          <Text>This machine</Text>
          <Text className="e-faint">The relay, setup and checks</Text>
        </div>
      </FlatRailItem>
    </div>
  );

  return (
    <ViewBody flush toolbar={<Text className="e-faint">Read-only · change a setting by running its command in your own terminal</Text>}>
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={24}>
        <div className="e-split-main e-settings">
          {error && !report && <Text className="e-status-txt">Could not read the settings: {error}</Text>}
          {loading && !report && <Text className="e-faint">Reading the settings…</Text>}
          {groupItems(items).map(([group, list]) => (
            <section key={group} className="e-settings-group">
              <h3 className="e-settings-title">{group}</h3>
              {list.map((it) => <SettingRow key={`${group}-${it.label}`} item={it} />)}
            </section>
          ))}
          {report && (
            <Text className="e-faint e-settings-foot">
              Changing settings here, like the Hermes dashboard's config page, is planned (see the
              Roadmap in Docs). Until then the dashboard only shows them, so nothing in a browser can
              change an agent's money or keys.
            </Text>
          )}
        </div>
      </SidebarLayout>
    </ViewBody>
  );
}
