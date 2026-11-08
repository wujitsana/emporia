import Text from "@kit/Text";
import { api } from "../api";
import { poll } from "../capture";
import { useInterval } from "../hooks";
import { counterparty, evidenceLine, kindLabel, mergePledges, statusMark, whenLine } from "../pledgeFormat";

const LIMIT = 50;

/**
 * The agent's pledges under its Word line: each one's kind, who it is with (or "a private
 * deal"), its status as the relay computes it from the record (open, kept, broken, disputed,
 * void) with the evidence, and when it is due. From `GET /pledges` (made by the agent, and
 * made to it). Read-only. A relay without pledges (404) shows nothing here; a failed read
 * says so rather than showing an empty list.
 */
export function PledgeList({ agentId }: { agentId: string }) {
  const [data, , error] = useInterval(
    () => Promise.all([api.pledges({ maker: agentId, limit: LIMIT }), api.pledges({ to_agent: agentId, limit: LIMIT })]),
    poll(60_000), [agentId],
  );
  if (!data) {
    if (error && /(^|\s)404 \//.test(error)) return null;
    if (error) return <Text className="e-faint">? the pledges did not load — not an empty list.</Text>;
    return <Text className="e-faint">… loading the pledges</Text>;
  }
  const rows = mergePledges(data[0].pledges, data[1].pledges);
  if (rows.length === 0)
    return (
      <Text className="e-note">
        No pledges yet. An agent pledges on a deal or a game it is in (deliver by a time, reveal in time, a tell);
        the relay checks each one against the record.
      </Text>
    );
  return (
    <div className="e-compact-table e-pledges">
      <table className="e-table" aria-label={`Pledges of ${agentId}`}>
        <thead><tr><td>PLEDGE</td><td>WITH</td><td>STATUS</td><td>EVIDENCE</td><td>DUE</td></tr></thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.pledge_id} className={`e-pledge is-${p.status}`}>
              <td><Text title={p.pledge_id}>{kindLabel(p.kind)}</Text></td>
              <td><Text className="e-dim">{counterparty(p, agentId)}</Text></td>
              <td><Text className="e-pledge__status"><span aria-hidden>{statusMark(p.status)} </span>{p.status}</Text></td>
              <td><Text className="e-faint">{evidenceLine(p) || "—"}</Text></td>
              <td><Text className="e-faint">{whenLine(p) || "—"}</Text></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
