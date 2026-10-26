/** P178: what the local console's GET /settings returns, and how the view groups it. */
export interface SettingItem {
  group: string;
  label: string;
  value: string;
  about: string;
  command: string;
  level: "ok" | "warn" | "danger";
  person_only: boolean;
}

export interface SettingsReport {
  agents: { agent_id: string; profile: string; items: SettingItem[] }[];
  machine: SettingItem[];
}

/** Items in their groups, in first-seen order. */
export function groupItems(items: SettingItem[]): [string, SettingItem[]][] {
  const groups = new Map<string, SettingItem[]>();
  for (const it of items) groups.set(it.group, [...(groups.get(it.group) ?? []), it]);
  return [...groups.entries()];
}
