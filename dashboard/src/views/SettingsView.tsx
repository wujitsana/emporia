/**
 * P178: every setting `emporia` changes — each agent's and this machine's —
 * laid out like Hermes's own config page: sections, one row per setting, its
 * value drawn the way Hermes's form would (a switch, the option chosen among
 * its options, a number with its unit), and where to change it.
 *
 * Read-only, and it stays so: the agent can reach this console from its own
 * terminal, and must never change its own limits. A setting Hermes's dashboard
 * edits links there (hermesPlace.ts); the rest show the command a person runs.
 * Read from the local console (GET /settings), which holds no secret in its
 * answer; a secret shows only as set or not set.
 */
import { useMemo, useState } from "react";
import Text from "@kit/Text";
import SidebarLayout from "@kit/SidebarLayout";
import { FlatRailItem } from "../ui/FlatRailItem";
import { CopyCommand } from "../ui/CopyCommand";
import { ViewBody } from "../ui/layout";
import { useInterval } from "../hooks";
import { consoleUrl, httpFetch, VIA_CONSOLE } from "../transport";
import { VIEWER_ID } from "../relayEnv";
import { EMPORIA_VERSION } from "../buildInfo";
import {
  changeInHermes, groupItems, kindOf, READ_ONLY_NOTE, secretState, settingsCommand, switchOn, type SettingItem, type SettingsReport,
} from "../settings";
import { hermesPlace, type HermesPlace } from "../hermesPlace";
import { api } from "../api";
import { useRelayCtx } from "../relayCtx";


const MACHINE = "__machine__";
const RELAY = "__relay__";

async function fetchSettings(): Promise<SettingsReport> {
  if (!VIA_CONSOLE) return { agents: [], machine: [] };
  const r = await httpFetch(consoleUrl("/settings"));
  if (!r.ok) throw new Error(`settings ${r.status}`);
  return r.json();
}


/** The value, drawn as Hermes's form would draw the field — but never a control: nothing here is pressed. */
export function SettingValue({ item }: { item: SettingItem }) {
  const kind = kindOf(item);
  if (kind === "switch") {
    const on = switchOn(item);
    return (
      <span className={`e-setting-plain${on ? " is-on" : ""}`} role="img" aria-label={`${item.label}: ${on ? "on" : "off"}`}>
        {on ? "On" : "Off"}
      </span>
    );
  }
  if (kind === "secret") {
    const set = secretState(item) === "set";
    return <span className={`e-setting-plain${set ? " is-on" : ""}`}>{set ? "set" : "not set"}</span>;
  }
  if (kind === "choice" && item.options?.length) {
    const current = item.current ?? item.value;
    const others = item.options.filter((o) => o !== current);
    return (
      <span className="e-setting-choices" aria-label={`${item.label}: ${current}`}>
        <span className="e-setting-plain is-on">{current}</span>
        {current === item.recommended && <span className="e-setting-rec e-faint"> · recommended</span>}
        {others.length > 0 && (
          <span className="e-setting-rec e-faint"> (other options: {others.join(", ")})</span>
        )}
      </span>
    );
  }
  if (!item.value) return null;
  const cls = kind === "number" || kind === "money" ? "e-setting-number" : kind === "list" ? "e-setting-list" : "e-setting-value";
  return <span className={cls}>{item.value}</span>;
}

function ChangeInHermes({ place }: { place: HermesPlace }) {
  if (place.href) {
    return (
      <a className="e-setting-hermes" href={place.href} target="_blank" rel="noopener noreferrer"
        title={place.text}>Change in Hermes ↗</a>
    );
  }
  if (place.open) {
    return (
      <button type="button" className="e-setting-hermes" onClick={place.open} title={place.text}>
        Change in Hermes →
      </button>
    );
  }
  return <span className="e-setting-hermes-text">Change in {place.text}</span>;
}

export function SettingRow({ item, place }: { item: SettingItem; place: HermesPlace }) {
  const inHermes = changeInHermes(item);
  const mark = item.level === "danger" ? "✗" : item.level === "warn" ? "!" : "";
  return (
    <div className={`e-setting e-setting-${item.level}`}>
      <div className="e-setting-main">
        <div className="e-setting-text">
          <span className="e-setting-label">
            {mark && <span className="e-setting-mark" aria-label={item.level === "danger" ? "risk" : "to look at"}>{mark}</span>}
            {item.label}
          </span>
          <span className="e-setting-about">{item.about}</span>
        </div>
        <div className="e-setting-control">
          <SettingValue item={item} />
          {item.is_default && <span className="e-setting-tag">default</span>}
        </div>
      </div>
      {(inHermes || item.command) && (
        <div className="e-setting-change">
          {inHermes && <ChangeInHermes place={place} />}
          {inHermes && <code className="e-setting-env" title="Its name in Hermes's form">{item.env}</code>}
          {item.command && <CopyCommand command={item.command} />}
          {item.person_only && (
            <span className="e-setting-note">
              Only a person runs this, in their own terminal — never through an agent.
            </span>
          )}
        </div>
      )}
    </div>
  );
}

export function SettingsSections({ items, profile }: { items: SettingItem[]; profile: string }) {
  const place = useMemo(() => hermesPlace(profile), [profile]);
  return (
    <>
      {groupItems(items).map(([group, list]) => (
        <section key={group} className="e-settings-group" aria-label={group}>
          <h3 className="e-settings-title">{group}</h3>
          <div className="e-settings-card">
            {list.map((it) => <SettingRow key={`${group}-${it.label}`} item={it} place={place} />)}
          </div>
        </section>
      ))}
    </>
  );
}

export function SettingsView() {
  const { isRelayOperator } = useRelayCtx();
  const [report, loading, error] = useInterval(fetchSettings, 60_000);
  // P179: the relay's own settings, for its operator — wherever the relay runs.
  const [relay] = useInterval(
    () => (isRelayOperator ? api.operatorOverview() : Promise.resolve(null)), 60_000, [isRelayOperator]);
  const [picked, setPicked] = useState<string | null>(null);
  const agents = useMemo(() => report?.agents ?? [], [report]);
  const selected = picked ?? (agents.find((a) => a.agent_id === VIEWER_ID)?.agent_id ?? agents[0]?.agent_id
    ?? (VIA_CONSOLE ? MACHINE : RELAY));
  const agent = agents.find((a) => a.agent_id === selected);
  const items = useMemo(
    () => (selected === RELAY ? relay?.settings : selected === MACHINE ? report?.machine : agent?.items) ?? [],
    [report, relay, agent, selected]);
  const warnings = (list: SettingItem[]) => list.filter((i) => i.level !== "ok").length;
  const title = selected === RELAY ? "This relay" : selected === MACHINE ? "This machine" : agent?.agent_id ?? "";

  if (!VIA_CONSOLE && !isRelayOperator) {
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
      {VIA_CONSOLE && (
        <FlatRailItem selected={selected === MACHINE} onClick={() => setPicked(MACHINE)}>
          <div className="e-docs-rail-item">
            <Text>This machine</Text>
            <Text className="e-faint">The relay it reads, setup and checks</Text>
          </div>
        </FlatRailItem>
      )}
      {isRelayOperator && (
        <FlatRailItem selected={selected === RELAY} onClick={() => setPicked(RELAY)}>
          <div className="e-docs-rail-item">
            <Text>This relay</Text>
            <Text className="e-faint">
              You run it{relay && warnings(relay.settings) ? ` · ${warnings(relay.settings)} to look at` : ""}
            </Text>
          </div>
        </FlatRailItem>
      )}
    </div>
  );

  return (
    <ViewBody flush toolbar={<Text className="e-faint">Settings · read-only · plugin {EMPORIA_VERSION}</Text>}>
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={24} initialPane="detail" backLabel="all settings">
        <div className="e-split-main e-settings">
          <p className="e-settings-intro">{READ_ONLY_NOTE}</p>
          {agent && (
            <p className="e-faint e-settings-hint">
              Change these: <code>{settingsCommand(agent.profile)}</code>
            </p>
          )}
          {title && <h2 className="e-settings-heading">{title}</h2>}
          {error && !report && <Text className="e-status-txt">Could not read the settings: {error}</Text>}
          {loading && !report && <Text className="e-faint">Reading the settings…</Text>}
          <SettingsSections items={items} profile={agent?.profile ?? ""} />
        </div>
      </SidebarLayout>
    </ViewBody>
  );
}
