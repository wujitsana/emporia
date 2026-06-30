import Indent from "@components/Indent";
import Text from "@components/Text";
import RowSpaceBetween from "@components/RowSpaceBetween";
import { api } from "../api";
import type { Settlement } from "../api";
import { useInterval } from "../hooks";
import { SlimCard } from "../ui/cards";
import { ViewBody } from "../ui/layout";
import { ViewStatus } from "../ui/ViewStatus";
import { AgentLink, SessionLink } from "../ui/chips";
import type { Navigate } from "../navigation";
import { VIEWER } from "../relayEnv";

function statusDot(status: string) {
  if (status === "settled") return <span style={{ color: "var(--theme-focused-foreground)" }}>●</span>;
  if (status === "refunded") return <span className="e-dim">○</span>;
  return <span className="e-faint">◌</span>;
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
    () => isOperator ? api.settlements() : api.agentSettlements(VIEWER ?? ""),
    10_000
  );
  const settlements: Settlement[] = data?.settlements ?? [];

  const totalStake = settlements.reduce((s, r) => s + r.total_stake_cents, 0);
  const totalFees = settlements.reduce((s, r) => s + r.platform_fee_cents, 0);
  const totalPayout = settlements.reduce((s, r) => s + r.winner_payout_cents, 0);
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
              <span className="e-kpi__val">{settlements.length}</span>
              <span className="e-kpi__lbl">settled</span>
            </div>
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
          <SlimCard title="Settlement history">
            <div className="e-compact-table" style={{ overflowX: "auto" }}>
              <table className="e-listings-native">
                <thead>
                  <tr>
                    <td>SESSION</td>
                    <td>TYPE</td>
                    <td>STATUS</td>
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
                          <RowSpaceBetween style={{ gap: 4 }}>
                            {statusDot(s.status)}
                            <Text className="e-dim">{s.status}</Text>
                          </RowSpaceBetween>
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
      </Indent>
    </ViewBody>
  );
}
