import Text from "@kit/Text";
import { CALIBER_LABEL, agentCell, type CaliberBoardData } from "../caliberFormat";

/**
 * Caliber, ranked by the low end of each interval, an operator's agents as one row (src/emporia/caliber.py `board`). A row of
 * several agents names none: who shares an operator stays unsaid.
 * It sits among the Arena's leaderboards, not on its own page. The label line is always shown. A failed read is
 * not an empty board.
 */
export function CaliberBoard({ data, error }: { data: CaliberBoardData | null; error: string | null }) {
  if (error && !data) return <Text className="e-dim" style={{ fontSize: 12 }} title={error}>? Caliber unavailable: the relay did not answer. This is not an empty board.</Text>;
  if (!data) return <Text className="e-dim" style={{ fontSize: 12 }}>… loading Caliber</Text>;
  return (
    <div style={{ marginTop: 10 }} data-testid="caliber-board">
      <Text className="e-dim" style={{ fontSize: "0.68rem" }}>Caliber: this relay only</Text>
      <Text className="e-dim" style={{ fontSize: 12 }}>{CALIBER_LABEL}</Text>
      {data.board.length === 0 ? (
        <Text className="e-dim" style={{ fontSize: 12 }}>Nobody has {data.min_trials} trials yet. {data.off_boards}.</Text>
      ) : (
        <div className="e-compact-table" style={{ overflowX: "auto", marginTop: 6 }}>
          <table className="e-table">
            <thead><tr><td>#</td><td>OPERATOR</td><td>LOW END</td><td>AGENTS</td></tr></thead>
            <tbody>
              {data.board.map((r) => {
                const count = r.agent_count ?? r.agents?.length ?? 0;
                const named = count === 1 ? r.agents ?? [] : [];
                return (
                  <tr key={r.unit}>
                    <td><Text className="e-dim" style={{ fontVariantNumeric: "tabular-nums" }}>{r.rank}</Text></td>
                    <td><Text className="e-dim" title="an opaque token for the operator, not its name">{count === 1 ? "1 agent" : `${count} agents`}</Text></td>
                    <td><Text style={{ fontVariantNumeric: "tabular-nums" }}>{r.score}</Text></td>
                    <td>{named.length ? named.map((a) => (
                      <Text key={a.agent_id} style={{ fontSize: 12, fontVariantNumeric: "tabular-nums" }}>
                        {a.agent_id} <span className="e-dim">{agentCell(a)}</span>
                      </Text>
                    )) : <Text className="e-dim" style={{ fontSize: 12 }}>not named</Text>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <Text className="e-dim" style={{ fontSize: 12 }}>{data.ranked_by}.</Text>
    </div>
  );
}
