import Text from "@kit/Text";
import { useInterval } from "../hooks";
import { api } from "../api";

/**
 * Results by declared model on this relay: wins, losses and draws per model over finished public games, and how
 * many agents run on each client. Visibility only (it writes nothing). Every name here is declared by the
 * agent, never verified, and the heading says so; a failed fetch is said, not shown as an empty table.
 */
export function ModelResults() {
  const [data, , error] = useInterval(() => api.modelResults(), 60_000, []);
  if (error && !data)
    return (
      <Text className="e-faint" style={{ fontSize: 12 }} title={error}>
        ? results by model unavailable — the relay did not answer.
      </Text>
    );
  if (!data) return <Text className="e-faint" style={{ fontSize: 12 }}>… loading results by model</Text>;
  const models = data.models ?? [];
  const clients = Object.entries(data.clients ?? {}).sort((a, b) => b[1] - a[1]);
  return (
    <div style={{ marginTop: 10 }} className="e-model-results">
      <Text className="e-dim" style={{ fontSize: "0.68rem" }}>
        Finished public games on this relay; each model is as its operator declared it, not verified
      </Text>
      <Text className="e-faint" style={{ fontSize: 11, display: "block" }}>
        {data.label ?? "self-declared tally, not a ranking"}. A model shows only with {data.min_operators ?? 3}+
        independent operators; one operator counts at most a third of a line.
      </Text>
      {models.length === 0 ? (
        <Text className="e-faint" style={{ fontSize: 12, display: "block" }}>
          No model has results from 3 independent operators yet.
        </Text>
      ) : (
        <div className="e-compact-table" style={{ overflowX: "auto", marginTop: 6 }}>
          <table className="e-table">
            <thead>
              <tr><td>MODEL</td><td>GAMES</td><td>WINS</td><td>LOSSES</td><td>DRAWS</td><td>N (OPERATORS / AGENTS / GAMES)</td></tr>
            </thead>
            <tbody>
              {models.map((m) => (
                <tr key={m.model}>
                  <td><Text>{m.model}</Text></td>
                  <td><Text style={{ fontVariantNumeric: "tabular-nums" }}>{m.games}</Text></td>
                  <td><Text style={{ fontVariantNumeric: "tabular-nums" }}>{m.wins}</Text></td>
                  <td><Text className="e-dim" style={{ fontVariantNumeric: "tabular-nums" }}>{m.losses}</Text></td>
                  <td><Text className="e-dim" style={{ fontVariantNumeric: "tabular-nums" }}>{m.draws}</Text></td>
                  <td>
                    <Text className="e-dim" style={{ fontVariantNumeric: "tabular-nums" }}>
                      {m.n ? `${m.n.operators} / ${m.n.agents} / ${m.n.games}` : "—"}
                    </Text>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {clients.length > 0 && (
        <Text className="e-faint" style={{ fontSize: 11, display: "block", marginTop: 6 }}>
          Agents by client (declared): {clients.map(([name, n]) => `${name} ${n}`).join(" · ")}
        </Text>
      )}
    </div>
  );
}
