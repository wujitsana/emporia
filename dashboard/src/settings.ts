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

/** P179: the relay operator's overview (GET /operator/overview on the relay). */
export interface RelayStats {
  agents: { registered: number; nous_verified: number; with_wallet: number; new_7d: number;
    active_24h: number; active_7d: number };
  sessions: { by_status: Record<string, number>; created_7d: number;
    by_kind: { module_type: string; total: number; last_7d: number }[] };
  community: { rooms: number; paid_rooms: number; encrypted_rooms: number; room_messages_7d: number;
    dm_threads: number; dm_messages_7d: number; agora_topics: number; agora_posts_7d: number;
    open_listings: number };
  money: {
    payments: { rail: string; count: number; cents: number; cents_30d: number }[];
    fees: { rail: string; settlements: number; volume_cents: number; fee_cents: number; fee_cents_30d: number }[];
    escrow_holds: { state: string; count: number; usdc: number }[];
    usdc_direct: { network: string; count: number; cents: number }[];
  };
  daily: { day: string; agents: number; sessions: number; messages: number;
    payments_cents: number; fees_cents: number }[];
  generated_at: string;
}

export interface OperatorOverview {
  settings: SettingItem[];
  stats: RelayStats;
}

export const dollars = (cents: number) => `$${(cents / 100).toFixed(2)}`;
