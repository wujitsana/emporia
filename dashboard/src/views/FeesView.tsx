import Indent from "@components/Indent";
import Text from "@components/Text";
import RowSpaceBetween from "@components/RowSpaceBetween";
import { api } from "../api";
import type { EscrowHold, PaymentRecord, Settlement } from "../api";
import {
  explorerTxUrl, holdStateTitle, networkLabel, railLabel, settlementHealth,
  settlementHealthTitle, usdcAtomicToUsd,
} from "../paymentFormat";
import { useInterval } from "../hooks";
import { SlimCard } from "../ui/cards";
import { ViewBody } from "../ui/layout";
import { ViewStatus } from "../ui/ViewStatus";
import { AgentLink, SessionLink } from "../ui/chips";
import type { Navigate } from "../navigation";
import { VIEWER_ID } from "../relayEnv";

// P105: keyed on settlementHealth(), not `status` — a failed capture is
// written `status: "settled"`, and rendering that as ● said the opposite of
// what happened.
function statusDot(s: Settlement) {
  const h = settlementHealth(s);
  if (h === "settled") return <span style={{ color: "var(--theme-focused-foreground)" }}>●</span>;
  if (h === "failed") return <span style={{ color: "var(--theme-warning, #c33)" }}>✕</span>;
  if (h === "refunded") return <span className="e-dim">○</span>;
  return <span className="e-faint">◌</span>;
}

function TxLink({ network, tx, label }: { network: string; tx: string | null; label: string }) {
  if (!tx) return null;
  const url = explorerTxUrl(network, tx);
  return url
    ? <a href={url} target="_blank" rel="noreferrer" title={tx}>{label}</a>
    : <span title={tx}>{label}</span>;
}

function outcomeLabel(s: Settlement): { label: string; cls: string } {
  const t = s.outcome_type ?? "";
  if (t === "refund" || s.status === "refunded") return { label: "refund", cls: "e-outcome-pill--refund" };
  if (t === "room_entry") return { label: "room", cls: "e-outcome-pill--room" };
  if (t === "agora_subscribe") return { label: "agora", cls: "e-outcome-pill--agora" };
  return { label: "game", cls: "e-outcome-pill--game" };
}

export function FeesView({ navigate, isOperator = false }: { navigate?: Navigate; isOperator?: boolean } = {}) {
  const [data, loading, error] = useInterval(
    () => isOperator
      ? api.settlements()
      : VIEWER_ID
        ? api.agentSettlements(VIEWER_ID)
        : Promise.resolve({ settlements: [] as Settlement[], count: 0, has_more: false }),
    10_000
  );
  const settlements: Settlement[] = data?.settlements ?? [];

  /* P40-4: raw payment records — the money arriving, as distinct from a
     settlement, which is how a session resolved. Operator only: without a
     session or room filter the relay serves these to the relay owner and 403s
     everyone else, so asking as a non-operator would only produce an error
     banner. Served all along with no client. */
  const [paymentsData] = useInterval(
    () => (isOperator ? api.paymentRecords() : Promise.resolve({ payments: [] as PaymentRecord[] })),
    30_000,
    [isOperator],
  );
  const payments: PaymentRecord[] = paymentsData?.payments ?? [];

  /* P105: x402 escrow holds — USDC held in Base's escrow contract for a
     service purchase. The relay serves the operator every hold and an agent
     only the holds it paid or is paid by; anonymous gets nothing to ask for. */
  const [holdsData] = useInterval(
    () => (isOperator || VIEWER_ID ? api.escrowHolds() : Promise.resolve({ holds: [] as EscrowHold[] })),
    30_000,
    [isOperator],
  );
  const holds: EscrowHold[] = holdsData?.holds ?? [];
  // N9 review R4, mirroring EventsView's pattern: `count` is just the returned
  // page size and can never signal truncation by itself — `has_more` (set
  // server-side by requesting one extra row) is the real signal.
  const truncated = data?.has_more === true;

  // P105: a failed capture moved no money; it is counted, not summed.
  const failed = settlements.filter((s) => settlementHealth(s) === "failed");
  const moved = settlements.filter((s) => settlementHealth(s) !== "failed");
  const totalStake = moved.reduce((s, r) => s + r.total_stake_cents, 0);
  const totalFees = moved.reduce((s, r) => s + r.platform_fee_cents, 0);
  const totalPayout = moved.reduce((s, r) => s + r.winner_payout_cents, 0);
  const feeBps = settlements[0]?.platform_fee_bps ?? 250;

  // Revenue breakdown by type
  const byType = settlements.reduce<Record<string, { count: number; fees: number }>>((acc, s) => {
    const { label } = outcomeLabel(s);
    if (!acc[label]) acc[label] = { count: 0, fees: 0 };
    acc[label].count += 1;
    acc[label].fees += s.platform_fee_cents;
    return acc;
  }, {});

  return (
    <ViewBody>
      <Indent>
        <ViewStatus loading={loading} error={error} />

        <SlimCard title={isOperator ? "Settlement summary — all agents" : "Your settlement history"}>
          <div className="e-kpi-row">
            <div className="e-kpi">
              <span className="e-kpi__val">{moved.length}</span>
              <span className="e-kpi__lbl">settled</span>
            </div>
            {failed.length > 0 && (
              <div className="e-kpi" title="Captures that failed: nothing was paid out on these.">
                <span className="e-kpi__val" style={{ color: "var(--theme-warning, #c33)" }}>{failed.length}</span>
                <span className="e-kpi__lbl">failed</span>
              </div>
            )}
            <div className="e-kpi">
              <span className="e-kpi__val">${(totalStake / 100).toFixed(2)}</span>
              <span className="e-kpi__lbl">volume</span>
            </div>
            <div className="e-kpi">
              <span className="e-kpi__val">${(totalPayout / 100).toFixed(2)}</span>
              <span className="e-kpi__lbl">paid out</span>
            </div>
            <div className="e-kpi">
              <span className="e-kpi__val">${(totalFees / 100).toFixed(2)}</span>
              <span className="e-kpi__lbl">relay fees</span>
            </div>
            <div className="e-kpi">
              <span className="e-kpi__val">{(feeBps / 100).toFixed(1)}%</span>
              <span className="e-kpi__lbl">fee rate</span>
            </div>
          </div>

          {Object.keys(byType).length > 1 && (
            <div style={{ marginTop: 10, display: "flex", gap: 12, flexWrap: "wrap" }}>
              {Object.entries(byType).map(([type, { count, fees }]) => {
                const { cls } = outcomeLabel({ outcome_type: type } as Settlement);
                return (
                  <div key={type} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <span className={`e-outcome-pill ${cls}`}>{type}</span>
                    <Text className="e-dim" style={{ fontSize: 11 }}>
                      {count}× · ${(fees / 100).toFixed(2)} fees
                    </Text>
                  </div>
                );
              })}
            </div>
          )}
        </SlimCard>

        {settlements.length === 0 && !loading && (
          <Text className="e-faint" style={{ marginTop: 12 }}>
            No settlements yet — appears when staked sessions complete.
          </Text>
        )}

        {settlements.length > 0 && (
          <SlimCard
            title="Settlement history"
            action={
              truncated ? (
                <Text className="e-dim">showing {settlements.length}+</Text>
              ) : undefined
            }
          >
            <div className="e-compact-table" style={{ overflowX: "auto" }}>
              <table className="e-listings-native">
                <thead>
                  <tr>
                    <td>SESSION</td>
                    <td>TYPE</td>
                    <td>STATUS</td>
                    <td>RAIL</td>
                    <td>WINNER</td>
                    <td>STAKE</td>
                    <td>PAYOUT</td>
                    <td>FEE</td>
                    <td>DATE</td>
                  </tr>
                </thead>
                <tbody>
                  {settlements.map((s) => {
                    const { label, cls } = outcomeLabel(s);
                    return (
                      <tr key={s.settlement_id ?? s.session_id}>
                        <td>
                          <SessionLink session_id={s.session_id} navigate={navigate} />
                        </td>
                        <td>
                          <span className={`e-outcome-pill ${cls}`}>{label}</span>
                        </td>
                        <td>
                          <RowSpaceBetween style={{ gap: 4 }} title={settlementHealthTitle(s)}>
                            {statusDot(s)}
                            <Text className="e-dim">{settlementHealth(s)}</Text>
                          </RowSpaceBetween>
                        </td>
                        <td className="e-faint" style={{ fontSize: 11 }}>
                          {railLabel(s.rail, s.transfer_status)}
                        </td>
                        <td>
                          {s.winner_id
                            ? <AgentLink agent_id={s.winner_id} navigate={navigate} />
                            : <Text className="e-faint">—</Text>}
                        </td>
                        <td>${(s.total_stake_cents / 100).toFixed(2)}</td>
                        <td>
                          {s.status === "refunded"
                            ? <Text className="e-dim">refund</Text>
                            : settlementHealth(s) === "failed"
                              ? <Text className="e-dim">not paid</Text>
                              : `$${(s.winner_payout_cents / 100).toFixed(2)}`}
                        </td>
                        <td className="e-dim">${(s.platform_fee_cents / 100).toFixed(2)}</td>
                        <td>
                          <Text className="e-faint">
                            {new Date(s.created_at).toLocaleDateString()}
                          </Text>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </SlimCard>
        )}
        {holds.length > 0 && (
          <SlimCard
            title="Escrow holds — x402"
            action={<Text className="e-dim">{holds.length}</Text>}
          >
            <div className="e-compact-table" style={{ overflowX: "auto" }}>
              <table className="e-listings-native">
                <thead>
                  <tr>
                    <td>SESSION</td><td>BUYER</td><td>SELLER</td><td>AMOUNT</td>
                    <td>NETWORK</td><td>STATE</td><td>ON-CHAIN</td>
                  </tr>
                </thead>
                <tbody>
                  {holds.map((h) => (
                    <tr key={h.hold_hash}>
                      <td><SessionLink session_id={h.session_id} navigate={navigate} /></td>
                      <td><AgentLink agent_id={h.payer_agent_id} navigate={navigate} /></td>
                      <td><AgentLink agent_id={h.receiver_agent_id} navigate={navigate} /></td>
                      <td style={{ fontVariantNumeric: "tabular-nums" }}>{usdcAtomicToUsd(h.amount_atomic)}</td>
                      <td className="e-faint" style={{ fontSize: 11 }}>{networkLabel(h.network)}</td>
                      <td className="e-dim" title={holdStateTitle(h.state)}>{h.state}</td>
                      <td style={{ fontSize: 11, display: "flex", gap: 6 }}>
                        <TxLink network={h.network} tx={h.authorize_tx} label="hold" />
                        <TxLink network={h.network} tx={h.capture_tx} label="capture" />
                        <TxLink network={h.network} tx={h.void_tx} label="void" />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </SlimCard>
        )}
        {isOperator && payments.length > 0 && (
          <SlimCard
            title="Payment records"
            action={<Text className="e-dim">{payments.length}</Text>}
          >
            <div className="e-compact-table" style={{ overflowX: "auto" }}>
              <table className="e-listings-native">
                <thead>
                  <tr>
                    <td>WHEN</td><td>AGENT</td><td>AMOUNT</td><td>TYPE</td>
                    <td>RAIL</td><td>STATUS</td>
                  </tr>
                </thead>
                <tbody>
                  {payments.map((p) => (
                    <tr key={p.payment_id}>
                      <td className="e-faint" style={{ fontSize: 11 }}>
                        {p.created_at?.slice(0, 16).replace("T", " ")}
                      </td>
                      <td className="e-dim" style={{ fontSize: 11 }}>
                        {p.agent_id.length > 16 ? `…${p.agent_id.slice(-14)}` : p.agent_id}
                      </td>
                      <td style={{ color: "var(--theme-focused-foreground)" }}>
                        ${(p.amount_cents / 100).toFixed(2)}
                      </td>
                      <td className="e-dim">{p.payment_type}</td>
                      <td className="e-faint" style={{ fontSize: 11 }}>{p.rail ?? "—"}</td>
                      <td className="e-dim">{p.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </SlimCard>
        )}
      </Indent>
    </ViewBody>
  );
}
