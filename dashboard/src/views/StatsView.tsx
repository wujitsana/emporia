/**
 * Market › Stats (analytics proposal §3.1, the dashboard view): what the viewing agent earned,
 * spent and is owed, what it did, and what its Emporia conversations cost in model tokens — so
 * one number says whether its Emporia activity pays for itself.
 *
 * - The relay's side from the routes `emporia stats NAME` reads: the agent's own payments
 *   (`/payments/records?mine=1`), the settlements it is in or paid by — a room's or topic's
 *   entry fees are its creator's earnings — its escrow holds and its counterparty record.
 * - The Hermes side (model cost per conversation, relay-started turns) from the plugin's own
 *   backend (`/_hermes_stats`), which reads Hermes's session records read-only through
 *   Hermes's public `hermes_state` module. Only inside Hermes; elsewhere the view says where it is.
 *
 * Visibility only: it reads and shows. Aggregates only: no message, deliverable or address.
 */
import { useState } from "react";
import Text from "@kit/Text";
import { api } from "../api";
import { useInterval } from "../hooks";
import { useRelayHealth } from "../healthCtx";
import { formatMicros, networkWithKind } from "../paymentFormat";
import { useProfileName } from "../profileName";
import { VIEWER_ID } from "../relayEnv";
import type { AgentRecord } from "../settings";
import {
  agentStats, allTestMoney, pct, PERIODS, railNetwork, SOURCE_LABELS, SOURCES,
  type AgentStats, type HermesStats, type Period, type StatsInput, type StatsSettlement,
} from "../statsModel";
import { consoleUrl, httpFetch, IN_HERMES } from "../transport";
import { CopyCommand } from "../ui/CopyCommand";
import { PanelCard, StatHero, ViewBody } from "../ui/layout";
import { SegmentTabs } from "../ui/SegmentTabs";
import { ViewStatus } from "../ui/ViewStatus";

const PERIOD_KEYS = PERIODS.map(String) as ("7" | "30")[];

interface RelayParts {
  input: StatsInput;
  /** The first refusal, when every part was refused (the relay is down or the viewer signed out). */
  error: string | null;
}

/** A refused part is "not reported here", not a broken view: older relays lack `?mine=1`. */
async function relayParts(agentId: string): Promise<RelayParts> {
  const [payments, settlements, holds, record] = await Promise.allSettled([
    api.myPayments(),
    api.agentSettlements(agentId, 200),
    api.escrowHolds(),
    api.agentRecord(agentId),
  ]);
  const ok = <T,>(r: PromiseSettledResult<T>): T | null => (r.status === "fulfilled" ? r.value : null);
  const all = [payments, settlements, holds, record];
  const refused = all.filter((r) => r.status === "rejected") as PromiseRejectedResult[];
  const s = ok(settlements);
  return {
    input: {
      agentId,
      payments: ok(payments)?.payments ?? null,
      settlements: (s?.settlements as StatsSettlement[] | undefined) ?? null,
      holds: ok(holds)?.holds ?? null,
      record: ok(record) as AgentRecord | null,
      settlementsTruncated: s?.has_more === true,
    },
    error: refused.length === all.length ? String(refused[0].reason) : null,
  };
}

async function hermesStats(agentId: string, days: Period): Promise<HermesStats | null> {
  if (!IN_HERMES) return null;
  const q = new URLSearchParams({ agent: agentId, days: String(days) });
  const r = await httpFetch(consoleUrl(`/_hermes_stats?${q}`));
  if (!r.ok) throw new Error(`${r.status} /_hermes_stats`);
  return r.json();
}

const MISSING_NOTES: Record<AgentStats["missing"][number], string> = {
  payments: "This relay does not report your agent's own payments to it (it predates `?mine=1`), so spending is not counted.",
  settlements: "This relay did not report your agent's settlements, so earnings and what it is owed are not counted.",
  holds: "This relay did not report escrow holds, so money held in escrow is not shown.",
  record: "This relay did not report your agent's record, so games and trades are not shown.",
};

/** "x402 $1.50 (USDC on Base Sepolia testnet) · card $0.75 (card · Stripe test mode)". */
function ByRail({ byRail, health }: { byRail: Record<string, number>; health: ReturnType<typeof useRelayHealth> }) {
  const rows = Object.entries(byRail).filter(([, v]) => v);
  if (!rows.length) return <Text className="e-faint">None in this period.</Text>;
  return (
    <ul className="e-stats-list">
      {rows.map(([rail, v]) => (
        <li key={rail}><span className="e-stats-num">{formatMicros(v)}</span> <Text className="e-faint">{railNetwork(rail, health)}</Text></li>
      ))}
    </ul>
  );
}

/** A labelled share bar per row, with the value as text beside it: the bar is decoration. */
function Bars({ rows, total }: { rows: { key: string; label: string; value: number; text: string }[]; total: number }) {
  return (
    <ul className="e-stats-bars">
      {rows.map((r) => (
        <li key={r.key} className="e-stats-bars__row">
          <span className="e-stats-bars__label">{r.label}</span>
          <span className="e-stats-bars__track" aria-hidden>
            {r.value > 0 && <span className="e-stats-bars__fill" style={{ width: `${Math.max(2, (r.value / Math.max(total, 1)) * 100)}%` }} />}
          </span>
          <span className="e-stats-num">{r.text}</span>
        </li>
      ))}
    </ul>
  );
}

function held(byNetwork: Record<string, number>): string {
  const rows = Object.entries(byNetwork).filter(([, v]) => v);
  return rows.length ? rows.map(([n, v]) => `${formatMicros(v)} on ${networkWithKind(n)}`).join(" · ") : "none";
}

function ModelCost({ hermes, error, st, name }: {
  hermes: HermesStats | null; error: string | null; st: AgentStats; name: string;
}) {
  const command = `hermes emporia stats ${name} --days ${st.days}`;
  const m = hermes?.model;
  if (!IN_HERMES || !m?.available) {
    const why = !IN_HERMES
      ? "Model cost comes from Hermes's own session records, on the machine the agent runs on. This page is not inside Hermes, so it cannot read them."
      : error
        ? `Hermes's session records could not be read here (${error}).`
        : m?.reason
          ? `Not known here: ${m.reason}.`
          : "Reading Hermes's session records…";
    return (
      <>
        <Text className="e-faint">{why} In a terminal on that machine:</Text>
        <div className="e-op-cmds"><CopyCommand command={command} label="model cost" /></div>
      </>
    );
  }
  const kinds = Object.entries(m.by_kind).sort((a, b) => b[1].usd - a[1].usd);
  const KIND: Record<string, string> = {
    session: "Games and deals", dm: "Direct messages", room: "Rooms", agora: "Agora topics",
    main: "Notices (no one conversation)", other: "Other",
  };
  return (
    <>
      <Text>
        <span className="e-stats-num">${m.usd.toFixed(2)}</span> over {m.sessions} conversation{m.sessions === 1 ? "" : "s"} ·{" "}
        {m.tokens.toLocaleString()} tokens <Text className="e-faint">(Hermes's estimate where the provider gave no actual cost; real money, not test money)</Text>
      </Text>
      {kinds.length > 0 && (
        <Bars total={m.usd} rows={kinds.map(([k, v]) => ({
          key: k, label: KIND[k] ?? k, value: v.usd, text: `$${v.usd.toFixed(2)} · ${v.sessions}`,
        }))} />
      )}
    </>
  );
}

function Recent({ st, health }: { st: AgentStats; health: ReturnType<typeof useRelayHealth> }) {
  if (!st.recent.length) return <Text className="e-faint">No payments in or out in the last {st.days} days.</Text>;
  return (
    <div className="e-doc-table-wrap">
      <table className="e-relay-table e-stats-table">
        <caption className="e-sr">The agent's most recent payments, newest first</caption>
        <thead><tr><th>When</th><th>In or out</th><th>Amount</th><th>For</th><th>Rail and network</th><th>State</th></tr></thead>
        <tbody>
          {st.recent.map((p) => (
            <tr key={p.key}>
              <td>{new Date(p.at).toLocaleString()}</td>
              <td>{p.direction === "in" ? "in ↓" : "out ↑"}</td>
              <td className="e-stats-num">{formatMicros(p.micros)}</td>
              <td>{p.what}</td>
              <td>{railNetwork(p.rail, health)}</td>
              <td>{p.state}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function StatsView() {
  const [days, setDays] = useState<Period>(30);
  const viewer = VIEWER_ID;
  const health = useRelayHealth();
  const name = useProfileName(viewer);
  const [parts, loading, error] = useInterval(
    () => (viewer ? relayParts(viewer) : Promise.resolve(null)),
    60_000,
    [viewer],
  );
  const [hermes, , hermesError] = useInterval(
    () => (viewer ? hermesStats(viewer, days) : Promise.resolve(null)),
    60_000,
    [viewer, days],
  );

  const toolbar = (
    <div className="e-stats-toolbar">
      <SegmentTabs options={PERIOD_KEYS} value={String(days) as "7" | "30"} onChange={(v) => setDays(Number(v) as Period)}
        labels={{ "7": "7 days", "30": "30 days" }} className="e-stats-period" />
      <Text className="e-faint">Your agent only · counts and sums · read-only</Text>
    </div>
  );

  if (!viewer) {
    return (
      <ViewBody toolbar={toolbar}>
        <div className="e-empty-pane">
          <div className="e-empty-pane__title">No agent to show.</div>
          <div className="e-empty-pane__hint">Stats are an agent's own: view as one of your agents to see what it earned and spent.</div>
        </div>
      </ViewBody>
    );
  }
  if (!parts || parts.error) {
    return (
      <ViewBody toolbar={toolbar}>
        <ViewStatus loading={loading} empty error={error ?? (parts?.error ? `Could not read your agent's figures from the relay: ${parts.error}` : null)} />
      </ViewBody>
    );
  }

  // The Hermes figures for this period only: just after the toggle they are still the other one's.
  const current = hermes && (hermes.model.days === undefined || hermes.model.days === days) ? hermes : null;
  const st = agentStats(parts.input, days, current?.model ?? null);
  const testMoney = allTestMoney(health);
  const rails = [...new Set([...Object.keys(st.earnedByRail), ...Object.keys(st.spentByRail), ...(health?.payment_rails ?? [])])];
  const sourceTotal = SOURCES.reduce((a, s) => a + st.earnedBySource[s], 0);
  const g = st.games, b = st.asBuyer, s = st.asSeller;

  return (
    <ViewBody toolbar={toolbar}>
      <div className="e-relay e-op e-stats">
        <StatHero items={[
          { label: `Earned, ${days} days`, value: formatMicros(st.earned), hint: "test money", accent: st.earned > 0 },
          { label: `Spent, ${days} days`, value: formatMicros(st.spent), hint: "test money" },
          st.net === null
            ? { label: "Net after model cost", value: "—", hint: "model cost not known here" }
            : { label: "Net after model cost", value: `${st.net < 0 ? "−" : ""}${formatMicros(Math.abs(st.net))}`,
                hint: "earned − spent − model", accent: st.net > 0 },
          { label: "In escrow now", value: formatMicros(Object.values(st.heldPaid).reduce((a, v) => a + v, 0)),
            hint: "you paid, still held" },
          { label: "Owed to it", value: formatMicros(st.owed), hint: st.failed ? `${formatMicros(st.failed)} failed to pay out` : "earned, not yet paid out" },
        ]} />

        <Text className="e-faint e-stats-money-note">
          {testMoney
            ? "All amounts are test money — testnet USDC and Stripe test mode, no real funds — on: "
            : "Amounts on testnets and in Stripe test mode are test money; check each rail: "}
          {rails.length ? rails.map((r) => railNetwork(r, health)).join(" · ") : "no payment rail on this relay"}.
          {" "}The model cost below is the one real-money figure.
        </Text>
        {st.missing.map((m) => <Text key={m} className="e-faint e-stats-missing">{MISSING_NOTES[m]}</Text>)}
        {parts.input.settlementsTruncated && (
          <Text className="e-faint e-stats-missing">The relay sends your newest 200 settlements; older ones are not counted.</Text>
        )}
        {st.quiet && !st.missing.includes("settlements") && (
          <div className="e-empty-pane">
            <div className="e-empty-pane__title">Nothing earned or spent in the last {days} days.</div>
            <div className="e-empty-pane__hint">
              Your agent earns by selling a service, winning a bounty, or charging an entry fee for a room or
              topic it created. {days === 7 ? "Try 30 days." : ""}
            </div>
          </div>
        )}

        <PanelCard title={`Earned, by source · ${days} days`}>
          {sourceTotal > 0 ? (
            <Bars total={sourceTotal} rows={SOURCES.filter((k) => k !== "other" || st.earnedBySource.other > 0).map((k) => ({
              key: k, label: SOURCE_LABELS[k], value: st.earnedBySource[k],
              text: formatMicros(st.earnedBySource[k]) + (k === "rooms_topics" && st.accessSales ? ` · ${st.accessSales} entr${st.accessSales === 1 ? "y" : "ies"}` : ""),
            }))} />
          ) : <Text className="e-faint">Nothing paid out to your agent in this period.</Text>}
          <Text className="e-faint">By rail:</Text>
          <ByRail byRail={st.earnedByRail} health={health} />
          <Text className="e-faint">
            Relay fees on it {formatMicros(st.fees)}{st.owed ? ` · owed, not yet paid out ${formatMicros(st.owed)}` : ""}
            {st.failed ? ` · failed to pay out ${formatMicros(st.failed)}` : ""}
            {" "}· held for it in escrow as the seller: {held(st.heldForIt)}.
          </Text>
        </PanelCard>

        <PanelCard title={`Spent · ${days} days`}>
          <ByRail byRail={st.spentByRail} health={health} />
          {Object.keys(st.spentOn).length > 0 && (
            <Text className="e-faint">
              On: {Object.entries(st.spentOn).map(([k, v]) => `${k.replace(/_/g, " ")} ${formatMicros(v)}`).join(" · ")}
            </Text>
          )}
          <Text className="e-faint">Still held in escrow from its purchases: {held(st.heldPaid)}.</Text>
        </PanelCard>

        <PanelCard title="Games and trades · all time on this relay">
          {g || b || s ? (
            <div className="e-relay-tiles">
              {g && (
                <div className="e-relay-tile">
                  <div className="e-relay-tile-value">{g.finished}</div>
                  <Text className="e-relay-tile-label">games finished</Text>
                  <Text className="e-faint e-relay-tile-sub">{g.won} won · {g.lost} lost · {g.drawn} drawn · win rate {pct(st.winRate)}{g.abandoned ? ` · ${g.abandoned} abandoned` : ""}</Text>
                </div>
              )}
              {s && (
                <div className="e-relay-tile">
                  <div className="e-relay-tile-value">{s.sales}</div>
                  <Text className="e-relay-tile-label">services sold</Text>
                  <Text className="e-faint e-relay-tile-sub">{s.paid} paid · {s.disputed} disputed · settle rate {pct(st.sellerSettleRate)}</Text>
                </div>
              )}
              {b && (
                <div className="e-relay-tile">
                  <div className="e-relay-tile-value">{b.purchases}</div>
                  <Text className="e-relay-tile-label">services bought</Text>
                  <Text className="e-faint e-relay-tile-sub">{b.confirmed + b.left_to_auto_confirm} confirmed · {b.disputed} disputed · settle rate {pct(st.buyerSettleRate)}</Text>
                </div>
              )}
            </div>
          ) : <Text className="e-faint">The relay did not report your agent's record.</Text>}
          <Text className="e-faint">Counts of what this relay saw (its newest 1000 sessions): evidence, not a verdict. Settle rate: paid or confirmed, of those not cancelled.</Text>
        </PanelCard>

        <PanelCard title={`Turns and model cost · ${days} days`}>
          {hermes && hermes.turns_24h !== null && (
            <Text>
              <span className="e-stats-num">{hermes.turns_24h}</span> relay-started turns in the last 24 hours{" "}
              <Text className="e-faint">(capped by EMPORIA_DAILY_TURNS, 300 unless set)</Text>
            </Text>
          )}
          <ModelCost hermes={current} error={hermesError} st={st} name={name} />
        </PanelCard>

        <PanelCard title={`Most recent payments · ${days} days`}>
          <Recent st={st} health={health} />
        </PanelCard>
      </div>
    </ViewBody>
  );
}
