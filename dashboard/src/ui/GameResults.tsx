import Text from "@kit/Text";
import { useInterval } from "../hooks";
import { api } from "../api";
import type { GameResult } from "../api";
import { shortAgent } from "../chessReplay";
import { verdict } from "../resultsFormat";
import { RelTime } from "./RelTime";

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
        <table className="e-table">
          <thead>
            <tr><td>ENDED</td><td>GAME</td><td>PLAYERS</td><td>RESULT</td></tr>
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
  return (
    <>
      <tr>
        <td><Text className="e-faint" style={{ fontSize: 11 }}>
          <RelTime at={r.ended_at} />
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
      </tr>
      {setPlay(r.module_type) === "emporia:blotto" ? <BlottoRounds r={r} /> : null}
      {setPlay(r.module_type) === "emporia:two-thirds" || setPlay(r.module_type) === "emporia:lowest-unique"
        ? <TwoThirdsPicks r={r} /> : null}
      <CallsLine r={r} />
      {setPlay(r.module_type) === "emporia:bargain" ? <BargainLine r={r} /> : null}
      {setPlay(r.module_type) === "emporia:liars-dice" || setPlay(r.module_type) === "emporia:cv-auction"
        ? <ChanceLine r={r} /> : null}
    </>
  );
}

/** A set play by its name, without the version: the live :v2 games and the retired :v1
 *  ones still in the history are drawn the same way (calls). */
function setPlay(moduleType: string): string {
  return moduleType.replace(/:v\d+$/, "");
}

/** Calls (`:v2`): each player's revealed call, its own chance of winning sealed with its first move. */
function CallsLine({ r }: { r: GameResult }) {
  const calls = Object.entries((r.record.calls as Record<string, number> | undefined) ?? {});
  const sealed = ((r.record.called as string[] | undefined) ?? []).filter((p) => !calls.some(([q]) => q === p));
  if (calls.length === 0 && sealed.length === 0) return null;
  return (
    <tr>
      <td />
      <td colSpan={4}>
        <Text className="e-faint" style={{ fontSize: 11, fontVariantNumeric: "tabular-nums" }}>
          calls: {[...calls.map(([p, c]) => `${shortAgent(p, 10)} ${c} %`), ...sealed.map((p) => `${shortAgent(p, 10)} unrevealed`)].join(" · ")}
        </Text>
      </td>
    </tr>
  );
}

type Goods = Record<string, number>;
const goods = (g: Goods | undefined) => ["silk", "spice", "amber"].map((k) => `${g?.[k] ?? 0} ${k}`).join(" ");

/** Bargain: how it ended and, once the hands are published, what each side really held. */
function BargainLine({ r }: { r: GameResult }) {
  const ended = r.record.ended as { how?: string; offer?: number } | null | undefined;
  const offers = (r.record.offers as { n: number; allocation: Record<string, Goods> }[] | undefined) ?? [];
  const hands = ((r.record.deal as { hands?: Record<string, { values?: Goods }> | null } | undefined)?.hands) ?? {};
  const taken = ended?.how === "accepted" ? offers.find((o) => o.n === ended.offer) : undefined;
  return (
    <tr>
      <td />
      <td colSpan={4}>
        <Text className="e-faint" style={{ fontSize: 11, display: "block", fontVariantNumeric: "tabular-nums" }}>
          {taken ? `deal on offer ${taken.n}: ${r.participants.map((p) => `${shortAgent(p, 10)} gets ${goods(taken.allocation[p])}`).join(" · ")}`
            : ended?.how === "declined" ? `no deal — the last offer was declined (${offers.length} offers)` : `${offers.length} offers, no deal`}
        </Text>
        {Object.keys(hands).length > 0 ? (
          <Text className="e-faint" style={{ fontSize: 11, display: "block", fontVariantNumeric: "tabular-nums" }}>
            held: {r.participants.map((p) => `${shortAgent(p, 10)} ${goods(hands[p]?.values)}`).join(" · ")}
          </Text>
        ) : null}
      </td>
    </tr>
  );
}

/** The games pack: once published, what every player really held, beside how it ended. */
function ChanceLine({ r }: { r: GameResult }) {
  const hands = ((r.record.deal as { hands?: Record<string, { values?: number | number[] }> | null } | undefined)?.hands) ?? {};
  if (Object.keys(hands).length === 0) return null;
  const dice = setPlay(r.module_type) === "emporia:liars-dice";
  return (
    <tr>
      <td />
      <td colSpan={4}>
        <Text className="e-faint" style={{ fontSize: 11, display: "block", fontVariantNumeric: "tabular-nums" }}>
          {dice ? "held" : "signals"}: {r.participants.map((p) => {
            const v = hands[p]?.values;
            return `${shortAgent(p, 10)} ${Array.isArray(v) ? v.join(" ") : v ?? "—"}`;
          }).join(" · ")}
        </Text>
      </td>
    </tr>
  );
}

function TwoThirdsPicks({ r }: { r: GameResult }) {
  const picks = Object.entries((r.record.guesses as Record<string, number> | undefined) ?? {});
  if (picks.length === 0) return null;
  const target = setPlay(r.module_type) === "emporia:two-thirds"
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
