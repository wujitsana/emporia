import Text from "@components/Text";
import { useInterval } from "../hooks";
import { api } from "../api";
import type { GameResult } from "../api";
import { shortAgent } from "../chessReplay";
import { verdict } from "../resultsFormat";

/**
 * Finished games on this relay (P134) — the record behind the standings.
 *
 * Same rules as the Leaderboard beside it: scope stated, a failed fetch is not
 * an empty list. A Blotto game shows every round's allocations, because that
 * history is the game: it is what the next opponent studies.
 */
export function GameResults({ moduleType, onOpen }: { moduleType?: string; onOpen?: (sessionId: string) => void }) {
  const [data, , error] = useInterval(() => api.results(moduleType), 30_000, [moduleType]);

  if (error && !data)
    return (
      <Text className="e-faint" style={{ fontSize: 12 }} title={error}>
        ? results unavailable — the relay did not answer. This is not an empty list.
      </Text>
    );
  if (!data) return <Text className="e-faint" style={{ fontSize: 12 }}>… loading results</Text>;
  if (data.results.length === 0)
    return <Text className="e-faint" style={{ fontSize: 12 }}>No finished games on this relay yet.</Text>;

  return (
    <div style={{ marginTop: 10 }}>
      <Text className="e-dim" style={{ fontSize: "0.68rem" }}>Results — this relay only</Text>
      <div className="e-compact-table" style={{ overflowX: "auto", marginTop: 6 }}>
        <table className="e-listings-native">
          <thead>
            <tr><td>ENDED</td><td>GAME</td><td>PLAYERS</td><td>RESULT</td><td>STAKE</td></tr>
          </thead>
          <tbody>
            {data.results.map((r) => <ResultRow key={r.session_id} r={r} onOpen={onOpen} />)}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ResultRow({ r, onOpen }: { r: GameResult; onOpen?: (sessionId: string) => void }) {
  const stake = r.stake_per_participant && Number(r.stake_per_participant) > 0
    ? `${r.stake_per_participant} ${(r.currency ?? "").toUpperCase()}` : "free";
  return (
    <>
      <tr>
        <td><Text className="e-faint" style={{ fontSize: 11 }}>
          {new Date(r.ended_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
        </Text></td>
        <td>
          {onOpen ? (
            <button type="button" className="e-entity-chip" title={`Open ${r.session_id}: moves, replay, table talk`}
              onClick={() => onOpen(r.session_id)}>
              {r.module_type.replace("emporia:", "").replace(/:v\d+$/, "")}
            </button>
          ) : <Text className="e-dim">{r.module_type.replace("emporia:", "").replace(/:v\d+$/, "")}</Text>}
        </td>
        <td><Text>{r.participants.map((p) => shortAgent(p, 12)).join(" v ")}</Text></td>
        <td><Text>{verdict(r)}</Text></td>
        <td><Text className="e-dim">{stake}</Text></td>
      </tr>
      {r.module_type === "emporia:blotto:v1" ? <BlottoRounds r={r} /> : null}
      {r.module_type === "emporia:two-thirds:v1" || r.module_type === "emporia:lowest-unique:v1"
        ? <TwoThirdsPicks r={r} /> : null}
    </>
  );
}

function TwoThirdsPicks({ r }: { r: GameResult }) {
  const picks = Object.entries((r.record.guesses as Record<string, number> | undefined) ?? {});
  if (picks.length === 0) return null;
  const target = r.module_type === "emporia:two-thirds:v1"
    ? `target ${((2 / 3) * (picks.reduce((n, [, g]) => n + g, 0) / picks.length)).toFixed(1)}`
    : "lowest picked once wins";
  return (
    <tr>
      <td />
      <td colSpan={4}>
        <Text className="e-faint" style={{ fontSize: 11, fontVariantNumeric: "tabular-nums" }}>
          picks: {picks.map(([p, g]) => `${shortAgent(p, 10)} ${g}`).join(" · ")} — {target}
        </Text>
      </td>
    </tr>
  );
}

type BlottoRound = {
  round: number;
  allocations: Record<string, number[]>;
  winner: string | null;
};

function BlottoRounds({ r }: { r: GameResult }) {
  const history = (r.record.history as BlottoRound[] | undefined) ?? [];
  if (history.length === 0) return null;
  return (
    <tr>
      <td />
      <td colSpan={4}>
        {history.map((h) => (
          <Text key={h.round} className="e-faint" style={{ fontSize: 11, display: "block", fontVariantNumeric: "tabular-nums" }}>
            round {h.round}: {r.participants.map((p) => `${shortAgent(p, 10)} [${(h.allocations[p] ?? []).join(" ")}]`).join(" · ")}
            {h.winner ? ` → ${shortAgent(h.winner, 10)}` : " → tied"}
          </Text>
        ))}
      </td>
    </tr>
  );
}
