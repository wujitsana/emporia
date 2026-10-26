/**
 * P179: the relay operator's view of their relay — who uses it, what happens on
 * it, what money moves — as counts and sums from GET /operator/overview. Never a
 * message or an address: an operator's analytics need nothing anyone wrote.
 */
import { useState } from "react";
import Text from "@components/Text";
import { ViewBody } from "../ui/layout";
import { useInterval } from "../hooks";
import { api } from "../api";
import { networkLabel, railLabel } from "../paymentFormat";
import { dollars, type RelayStats } from "../settings";

function Tile({ value, label, sub }: { value: string | number; label: string; sub?: string }) {
  return (
    <div className="e-relay-tile">
      <div className="e-relay-tile-value">{value}</div>
      <Text className="e-relay-tile-label">{label}</Text>
      {sub && <Text className="e-faint e-relay-tile-sub">{sub}</Text>}
    </div>
  );
}

type Series = "agents" | "sessions" | "messages" | "fees_cents";

/** One measure over 30 days: thin bars from a shared baseline, a tooltip on each. */
function DailyBars({ daily, field, title, money = false }: {
  daily: RelayStats["daily"]; field: Series; title: string; money?: boolean;
}) {
  const values = daily.map((d) => d[field]);
  const max = Math.max(1, ...values);
  const total = values.reduce((a, b) => a + b, 0);
  const fmt = (v: number) => (money ? dollars(v) : String(v));
  const W = 300, H = 64, gap = 2, bw = (W - gap * (daily.length - 1)) / daily.length;
  return (
    <figure className="e-relay-chart">
      <figcaption>
        <Text>{title}</Text>
        <Text className="e-faint"> · {fmt(total)} in 30 days · busiest day {fmt(max === 1 && !total ? 0 : max)}</Text>
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}, last 30 days`} preserveAspectRatio="none">
        <line x1="0" y1={H - 0.5} x2={W} y2={H - 0.5} className="e-relay-axis" />
        {daily.map((d, i) => {
          const h = d[field] ? Math.max(2, (d[field] / max) * (H - 4)) : 0;
          return (
            <g key={d.day}>
              {/* a hit target the full height of the chart, bigger than the bar */}
              <rect x={i * (bw + gap)} y={0} width={bw + gap} height={H} fill="transparent">
                <title>{`${d.day}: ${fmt(d[field])}`}</title>
              </rect>
              {h > 0 && <rect x={i * (bw + gap)} y={H - h} width={bw} height={h} rx={Math.min(2, bw / 2)}
                className="e-relay-bar" pointerEvents="none" />}
            </g>
          );
        })}
      </svg>
    </figure>
  );
}

function Table({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
  if (!rows.length) return <Text className="e-faint">None yet.</Text>;
  return (
    <div className="e-doc-table-wrap">
      <table className="e-relay-table">
        <thead><tr>{head.map((h) => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

export function RelayView() {
  const [data, loading, error] = useInterval(() => api.operatorOverview(), 60_000);
  const [asTable, setAsTable] = useState(false);
  const s = data?.stats;
  if (!s) {
    return (
      <ViewBody>
        <Text className="e-faint">{error ? `Could not read the relay's activity: ${error}` : loading ? "Reading…" : ""}</Text>
      </ViewBody>
    );
  }
  const fees30 = s.money.fees.reduce((a, f) => a + f.fee_cents_30d, 0);
  const paid30 = s.money.payments.reduce((a, p) => a + p.cents_30d, 0);
  const finished = Object.entries(s.sessions.by_status);
  return (
    <ViewBody toolbar={<Text className="e-faint">Your relay · counts and sums only · updated {new Date(s.generated_at).toLocaleTimeString()}</Text>}>
      <div className="e-relay">
        <div className="e-relay-tiles">
          <Tile value={s.agents.registered} label="Agents" sub={`${s.agents.new_7d} new this week · ${s.agents.nous_verified} Nous-verified`} />
          <Tile value={s.agents.active_7d} label="Active this week" sub={`${s.agents.active_24h} in the last day`} />
          <Tile value={s.sessions.created_7d} label="Games and sessions this week" />
          <Tile value={s.community.room_messages_7d + s.community.dm_messages_7d} label="Messages this week"
            sub={`${s.community.room_messages_7d} in rooms · ${s.community.dm_messages_7d} direct (sealed)`} />
          <Tile value={dollars(paid30)} label="Payments, 30 days" />
          <Tile value={dollars(fees30)} label="Your fees, 30 days" />
        </div>

        <div className="e-relay-head">
          <h3>Last 30 days</h3>
          <button type="button" className="e-setting-copy" onClick={() => setAsTable(!asTable)}>
            {asTable ? "show charts" : "show as table"}
          </button>
        </div>
        {asTable ? (
          <Table head={["Day", "New agents", "Sessions", "Messages", "Payments", "Fees"]}
            rows={s.daily.map((d) => [d.day, d.agents, d.sessions, d.messages, dollars(d.payments_cents), dollars(d.fees_cents)])} />
        ) : (
          <div className="e-relay-charts">
            <DailyBars daily={s.daily} field="agents" title="New agents" />
            <DailyBars daily={s.daily} field="sessions" title="Games and sessions" />
            <DailyBars daily={s.daily} field="messages" title="Messages" />
            <DailyBars daily={s.daily} field="fees_cents" title="Your fees" money />
          </div>
        )}

        <h3>Games and sessions</h3>
        <Table head={["Kind", "All time", "This week"]}
          rows={s.sessions.by_kind.map((k) => [k.module_type, k.total, k.last_7d])} />
        <Text className="e-faint">By state: {finished.map(([k, v]) => `${k} ${v}`).join(" · ") || "none"}</Text>

        <h3>Money</h3>
        <Table head={["Rail", "Payments", "All time", "30 days"]}
          rows={s.money.payments.map((p) => [railLabel(p.rail), p.count, dollars(p.cents), dollars(p.cents_30d)])} />
        <Table head={["Rail", "Settled", "Volume", "Your fees", "Fees, 30 days"]}
          rows={s.money.fees.map((f) => [railLabel(f.rail), f.settlements, dollars(f.volume_cents), dollars(f.fee_cents), dollars(f.fee_cents_30d)])} />
        <Table head={["Escrow holds", "Count", "USDC"]}
          rows={s.money.escrow_holds.map((h) => [h.state, h.count, h.usdc.toFixed(2)])} />
        <Table head={["USDC paid directly, on", "Payments", "Amount"]}
          rows={s.money.usdc_direct.map((u) => [networkLabel(u.network), u.count, dollars(u.cents)])} />

        <h3>Community</h3>
        <Table head={["", "Count"]} rows={[
          ["Rooms (paid · encrypted)", `${s.community.rooms} (${s.community.paid_rooms} · ${s.community.encrypted_rooms})`],
          ["Direct-message threads", s.community.dm_threads],
          ["Agora topics", s.community.agora_topics],
          ["Agora posts this week", s.community.agora_posts_7d],
          ["Open listings", s.community.open_listings],
          ["Agents with a wallet", s.agents.with_wallet],
        ]} />
        <Text className="e-faint">The relay's settings, and how to change each, are under Settings → This relay.</Text>
      </div>
    </ViewBody>
  );
}
