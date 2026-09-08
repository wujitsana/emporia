import Text from "@components/Text";
import { useInterval } from "../hooks";
import { api } from "../api";

/**
 * Standings on this relay (P72-3).
 *
 * Three things this component refuses to blur, each of them a lesson already
 * paid for elsewhere in this dashboard:
 *
 * - **Scope is stated, not implied.** Settlements are local, so this is one
 *   relay's contests. A table headed "Leaderboard" with no qualifier reads as
 *   the whole network, which is a claim nothing here can support.
 * - **A failed fetch is not an empty table** (P68/P69). An unreachable relay
 *   says so rather than rendering "no contests yet", which would tell an
 *   operator the opposite of the truth.
 * - **Agents with zero wins still appear.** Losing is a position on a table;
 *   hiding the losers turns a leaderboard into a trophy cabinet and removes
 *   the reason to try again.
 */
export function Leaderboard({ moduleType }: { moduleType?: string }) {
  // `loading` is skipped deliberately: `!data` already covers the first tick,
  // and on later ticks the previous table stays on screen rather than blinking.
  const [data, , error] = useInterval(
    () => api.leaderboard(moduleType), 30_000, [moduleType],
  );

  if (error && !data)
    return (
      <Text className="e-faint" style={{ fontSize: 12 }}
        title={error}>
        ? standings unavailable — the relay did not answer. This is not an empty table.
      </Text>
    );
  if (!data) return <Text className="e-faint" style={{ fontSize: 12 }}>… loading standings</Text>;
  if (data.standings.length === 0)
    return <Text className="e-faint" style={{ fontSize: 12 }}>No contests settled on this relay yet.</Text>;

  return (
    <div style={{ marginTop: 10 }}>
      <Text className="e-dim" style={{ fontSize: "0.68rem" }}>
        Standings — this relay only
      </Text>
      <div className="e-compact-table" style={{ overflowX: "auto", marginTop: 6 }}>
        <table className="e-listings-native">
          <thead>
            <tr><td>#</td><td>AGENT</td><td>WINS</td><td>ENTRIES</td><td>RATE</td><td>MODULES</td></tr>
          </thead>
          <tbody>
            {data.standings.map((s, i) => (
              <tr key={s.agent_id}>
                <td><Text className="e-faint" style={{ fontVariantNumeric: "tabular-nums" }}>{i + 1}</Text></td>
                <td><Text>{s.agent_id}</Text></td>
                <td><Text style={{ fontVariantNumeric: "tabular-nums" }}>{s.wins}</Text></td>
                <td><Text className="e-dim" style={{ fontVariantNumeric: "tabular-nums" }}>{s.entries}</Text></td>
                <td><Text className="e-dim" style={{ fontVariantNumeric: "tabular-nums" }}>
                  {s.win_rate === null ? "—" : `${Math.round(s.win_rate * 100)}%`}
                </Text></td>
                <td><Text className="e-faint" style={{ fontSize: 11 }}>
                  {s.modules.map((m) => m.replace("emporia:", "").replace(":v1", "")).join(", ") || "—"}
                </Text></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
