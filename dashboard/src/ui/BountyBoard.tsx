import Badge from "@components/Badge";
import Text from "@components/Text";
import { useInterval } from "../hooks";
import { api } from "../api";
import { bountyStateLabel } from "../paymentFormat";

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
        Open bounties — a held prize is reserved by the funder; a promised one is not
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
                  <Badge
                    style={{ fontVariantNumeric: "tabular-nums" }}
                    title={
                      b.escrow_strength === "held"
                        ? "Held — the funder's money is reserved and cannot be spent elsewhere."
                        : b.escrow_strength === "promised"
                          ? "Promised — nothing is set aside. The funder pays after the result, and is flagged if they do not. Competing here is a credit risk."
                          : "This bounty predates escrow-strength recording."
                    }
                  >
                    {b.prize_cents === null ? "—" : `$${(b.prize_cents / 100).toFixed(2)}`}
                    {b.escrow_strength === "promised" ? " ⚠" : ""}
                  </Badge>
                </td>
                <td><Text className="e-dim" style={{ fontSize: 11 }}>
                  {b.module_type.replace("emporia:", "").replace(":v1", "")}
                </Text></td>
                <td>
                  {/* P79: seats are null when this relay does not have the
                      judging module — a federated bounty from a peer, or a
                      module removed after its bounties existed. "3/?" is
                      honest; "3/3" would invent a denominator. */}
                  <Text
                    style={{ fontVariantNumeric: "tabular-nums" }}
                    className={
                      b.max_entrants !== null && b.entrant_count >= b.max_entrants
                        ? undefined : "e-dim"
                    }
                    title={
                      b.max_entrants === null
                        ? "This relay does not have this bounty's judging module, so its seat count is unknown."
                        : b.entrant_count >= b.max_entrants
                          ? "Full — entry is closed."
                          : `Needs ${b.min_entrants} to run; ${b.max_entrants - b.entrant_count} seat(s) left.`
                    }
                  >
                    {b.entrant_count}/{b.max_entrants ?? "?"}
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
                  ) : (() => {
                    // P105: every state the relay writes has a label — a
                    // defaulted or unfunded prize used to print its raw key
                    // in the same faint text as a healthy one.
                    const st = bountyStateLabel(
                      b.state, b.max_entrants !== null && b.entrant_count >= b.max_entrants);
                    return (
                      <Text
                        className={st.failed ? undefined : "e-faint"}
                        style={{ fontSize: 11, ...(st.failed ? { color: "var(--theme-warning, #c33)" } : {}) }}
                        title={st.title}
                      >
                        {st.label}
                      </Text>
                    );
                  })()}
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
