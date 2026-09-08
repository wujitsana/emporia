import Badge from "@components/Badge";
import Text from "@components/Text";
import { useInterval } from "../hooks";
import { api } from "../api";

/**
 * Open contests (P72-4).
 *
 * Sits in the Listings view because a bounty *is* a listing subtype — same
 * table, same federation gossip — and because the primary nav's hotkeys are
 * fully allocated.
 *
 * `prize` is rendered as money offered, never as a price. A bounty inverts the
 * usual listing: the poster is paying, not charging, and a reader who mistakes
 * one for the other has misread the whole board.
 *
 * P72-8: seats and state are shown because the first version showed neither,
 * and both omissions made the board say the opposite of the truth. "2 entrants"
 * with no denominator reads as some interest when it means full and closed; a
 * running or cancelled bounty rendered identically to one still open for entry.
 * Same failure as a rejected fetch rendering as an empty list (P68/P69) — the
 * reassuring reading, chosen by omission.
 */
export function BountyBoard() {
  // Skipping `loading`: `!data` covers the first tick and later ticks keep the
  // previous board on screen instead of blinking.
  const [data, , error] = useInterval(() => api.bounties(), 20_000, []);

  if (error && !data)
    return (
      <Text className="e-faint" style={{ fontSize: 12 }} title={error}>
        ? bounties unavailable — the relay did not answer. This is not "no open contests".
      </Text>
    );
  if (!data) return <Text className="e-faint" style={{ fontSize: 12 }}>… loading bounties</Text>;
  if (data.bounties.length === 0)
    return <Text className="e-faint" style={{ fontSize: 12 }}>No open bounties on this relay.</Text>;

  return (
    <div style={{ marginTop: 10 }}>
      <Text className="e-dim" style={{ fontSize: "0.68rem" }}>
        Open bounties — prize is escrowed by the funder
      </Text>
      <div className="e-compact-table" style={{ overflowX: "auto", marginTop: 6 }}>
        <table className="e-listings-native">
          <thead>
            <tr><td>TITLE</td><td>PRIZE</td><td>MODULE</td><td>SEATS</td><td>STATE</td><td>CLOSES</td><td>FUNDER</td></tr>
          </thead>
          <tbody>
            {data.bounties.map((b) => (
              <tr key={b.listing_id}>
                <td><Text>{b.title}</Text></td>
                <td>
                  <Badge style={{ fontVariantNumeric: "tabular-nums" }}>
                    {b.prize_cents === null ? "—" : `$${(b.prize_cents / 100).toFixed(2)}`}
                  </Badge>
                </td>
                <td><Text className="e-dim" style={{ fontSize: 11 }}>
                  {b.module_type.replace("emporia:", "").replace(":v1", "")}
                </Text></td>
                <td>
                  <Text
                    style={{ fontVariantNumeric: "tabular-nums" }}
                    className={b.entrant_count >= b.max_entrants ? undefined : "e-dim"}
                    title={
                      b.entrant_count >= b.max_entrants
                        ? "Full — entry is closed."
                        : `Needs ${b.min_entrants} to run; ${b.max_entrants - b.entrant_count} seat(s) left.`
                    }
                  >
                    {b.entrant_count}/{b.max_entrants}
                  </Text>
                </td>
                <td>
                  {b.state === "running" && b.session_id ? (
                    <Text
                      style={{ fontSize: 11, color: "var(--theme-focused-foreground)" }}
                      title={`Playing now in session ${b.session_id}`}
                    >
                      running
                    </Text>
                  ) : (
                    <Text
                      className="e-faint"
                      style={{ fontSize: 11 }}
                      title={
                        b.state === "open"
                          ? b.entrant_count >= b.max_entrants
                            ? "Seats are full; waiting to be started."
                            : "Open for entry."
                          : `This bounty is ${b.state}.`
                      }
                    >
                      {b.state === "open" && b.entrant_count >= b.max_entrants ? "ready" : b.state}
                    </Text>
                  )}
                </td>
                <td><Text className="e-faint" style={{ fontSize: 11 }}>
                  {b.entry_deadline ? new Date(b.entry_deadline).toLocaleString() : "—"}
                </Text></td>
                <td><Text className="e-faint" style={{ fontSize: 11 }}>{b.funder_id}</Text></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
