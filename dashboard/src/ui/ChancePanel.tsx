/**
 * Liar's dice and the common-value auction (the games pack) as a viewer sees them: who is
 * seated, the bids in order, whose turn and the clock; after the end every die or signal beside
 * what was said at the table, the result and each score.
 *
 * Visibility only: the dashboard never bids or calls. While the game runs nothing is hidden here
 * because nothing is known here: the relay holds no die and no signal (`emporia:rand:v1`). A player's
 * next move is shown as the MCP call to copy.
 */
import Text from "@kit/Text";
import type { CSSProperties } from "react";
import { api } from "../api";
import type { Session } from "../api";
import { chanceView, type AuctionView, type ChanceView, type DiceView } from "../chance";
import { useInterval } from "../hooks";
import { VIEWER_ID } from "../relayEnv";
import { whenText } from "../relayFormat";
import { clockText } from "../setPlay";
import { AuditBadge } from "./AuditBadge";
import { GameChat } from "./GameChat";
import { McpCommandHint } from "./McpCommandHint";
import { VerifyBadge } from "./VerifyBadge";

const num: CSSProperties = { fontVariantNumeric: "tabular-nums" };

export function ChancePanel({ session: initial }: { session: Session }) {
  const id = initial.session_id;
  const running = initial.status === "active" || initial.status === "waiting";
  const [fresh] = useInterval(() => api.session(id), running ? 4_000 : 3_600_000, [id, running]);
  const [acts] = useInterval(() => api.sessionActions(id), running ? 4_000 : 3_600_000, [id, running]);
  const session = fresh ?? initial;
  const v = chanceView(session, acts?.actions);
  if (!v) return null;
  const live = session.status === "active" || session.status === "waiting";
  const clock = clockText(v.secondsLeft);
  const name = v.kind === "dice" ? "liar's dice" : "common-value auction";

  return (
    <div className="e-setplay e-chance">
      <header className="e-setplay__head">
        <div>
          <p className="e-setplay__game">{name}</p>
          <p className="e-setplay__players">{session.participants.join(" v ")}</p>
        </div>
        <span className={`e-setplay__status${live ? " is-live" : ""}`}>
          {live ? (session.status === "waiting" ? "waiting" : "live") : "finished"}
        </span>
      </header>

      <p className="e-faint e-blotto__rules" style={num}>
        {v.kind === "dice"
          ? `${v.players} players · ${v.diceEach} dice each, under cups only their owners can read · bids rise (more dice, or a higher face)${v.onesWild ? " · ones are wild" : ""} · one call ends the play`
          : `${v.players} players · one pot worth the same to all, known to nobody · each player is told a private noisy signal (±${v.spread} parts) · one sealed bid each · the highest bid wins value minus bid`}
        {" "}· drawn from a drand round and each player's own secret: the relay holds none · no stakes
      </p>

      {v.result ? <Verdict v={v} /> : (
        <>
          <p className="e-setplay__wait">
            {session.status === "waiting" ? `Waiting for players (${session.participants.length}/${v.players}).` : progress(v, clock)}
          </p>
          <span className="e-sr" role="status">{session.status === "waiting" ? "Waiting for players." : progress(v)}</span>
        </>
      )}

      {v.kind === "dice" ? <Bids v={v} /> : <Auction v={v} participants={session.participants} />}
      <Seats v={v} participants={session.participants} over={!!v.result} />

      <div className="e-setplay__meta">
        <Text className="e-faint" style={{ fontSize: 11 }}>
          …{id.slice(-10)}{session.created_at ? ` · started ${whenText(session.created_at)}` : ""} · no stakes
        </Text>
        <span className="e-setplay__badges">
          <AuditBadge sessionId={id} />
          <VerifyBadge sessionId={id} live={session.status === "active"} />
        </span>
      </div>

      <NextMoveHint session={session} v={v} />
      <GameChat sessionId={id} live={live} />
    </div>
  );
}

function progress(v: ChanceView, clock?: string | null): string {
  const tail = clock ? ` · ${clock}` : "";
  if (v.kind === "dice") {
    if (v.call) return `${v.call.by} called liar on ${v.call.bidder}'s bid: everyone reveals its secret${tail}.`;
    return v.toMove ? `${v.toMove}'s turn${tail} · ${v.bids.length} bid${v.bids.length === 1 ? "" : "s"} so far.`
      : "The beacon round is not read yet: bidding opens when it is published.";
  }
  if (v.phase === "commit") return `Sealing bids: ${v.committed.length} of ${v.players} in${tail}.`;
  if (v.phase === "reveal") return `Revealing bids: ${Object.keys(v.bids).length} of ${v.players}${tail}.`;
  return `Bids are in. Everyone reveals its secret: ${v.secrets.length} of ${v.players}.`;
}

function Verdict({ v }: { v: ChanceView }) {
  const r = v.result!;
  const detail = v.kind === "dice" && r.bid
    ? ` — ${(r.bid as { quantity: number; face: number }).quantity} × ${(r.bid as { quantity: number; face: number }).face} was ${r.termination === "bid_held" ? "true" : "a lie"} (${r.count} showed)`
    : v.kind === "auction" && typeof r.value === "number"
      ? ` — the pot was worth ${r.value}; ${r.bidder} bid ${r.winning_bid}${r.termination === "winners_curse" ? ": the winner's curse" : ""}`
      : r.reason ? ` — ${r.reason}` : "";
  return (
    <p className={`e-setplay__verdict${r.winner ? " is-win" : ""}`} role="status">
      {r.winner ? <><span aria-hidden>★ </span>{r.winner} wins</> : (r.termination ?? "over").replace(/_/g, " ")}
      {detail}
      {r.points ? (
        <span className="e-setplay__why" style={num}>{" "}— {Object.entries(r.points).map(([p, n]) => `${p} ${n}`).join(" · ")}</span>
      ) : null}
    </p>
  );
}

function Bids({ v }: { v: DiceView }) {
  if (v.bids.length === 0) return <Text className="e-note">No bid yet. {v.order[0] ? `${v.order[0]} opens.` : ""}</Text>;
  return (
    <div className="e-blotto__scroll">
      <table className="e-blotto__grid">
        <thead><tr><th scope="col">#</th><th scope="col">by</th><th scope="col">claims at least</th></tr></thead>
        <tbody>
          {v.bids.map((b) => {
            const called = v.call?.bid === b.n;
            return (
              <tr key={b.n} className={called ? "is-winner" : undefined}>
                <td style={num}>{b.n}</td><th scope="row">{b.by}</th>
                <td style={num}>{b.quantity} × {b.face}{called ? <span className="e-faint"> · called</span> : null}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Auction({ v, participants }: { v: AuctionView; participants: string[] }) {
  return (
    <p className="e-faint" style={{ fontSize: 12, ...num }}>
      {v.base !== null ? `public base value ${v.base} · ` : ""}
      {participants.map((p) => `${p}: ${p in v.bids ? `bid ${v.bids[p]}` : v.committed.includes(p) ? "sealed" : "no bid yet"}`).join(" · ")}
    </p>
  );
}

/** Nothing is hidden by this view: nothing is known to it. After the end, every die or signal. */
function Seats({ v, participants, over }: { v: ChanceView; participants: string[]; over: boolean }) {
  return (
    <div className="e-seats" role="list" aria-label="Players">
      {participants.map((p) => {
        const mine = v.kind === "dice" ? v.dice[p] : v.signals[p];
        const shown = over && mine !== undefined && (!Array.isArray(mine) || mine.length > 0);
        return (
          <div role="listitem" key={p} className="e-seat">
            <span className="e-seat__name">{p}{p === VIEWER_ID ? " (you)" : ""}</span>
            <span className="e-seat__state" style={num}>
              {shown ? (v.kind === "dice" ? <>held {(mine as number[]).join(" ")}</> : <>signal {mine as number}</>)
                : over ? "never revealed" : v.kind === "dice" ? "▒▒ under the cup" : "▒▒ signal is private"}
            </span>
            <span className="e-seat__move" style={num}>{v.result?.points?.[p] ?? ""}</span>
          </div>
        );
      })}
    </div>
  );
}

function NextMoveHint({ session, v }: { session: Session; v: ChanceView }) {
  if (!VIEWER_ID || session.status !== "active" || v.result) return null;
  if (v.kind === "dice") {
    if (v.call) return <McpCommandHint label="Your agent reveals its secret with" tool="submit_action"
      args={{ session_id: session.session_id, action_type: "reveal", payload: "{}" }} />;
    if (v.toMove !== VIEWER_ID) return null;
    return <McpCommandHint label="Your agent's move — it bids (or calls the last bid) with" tool="submit_action"
      args={{ session_id: session.session_id, action_type: "bid | liar", payload: '{"quantity": 4, "face": 3}', say: "(table talk)" }} />;
  }
  if (v.phase === "commit" && !v.committed.includes(VIEWER_ID)) return <McpCommandHint
    label="Your agent seals its bid with" tool="commit_move" args={{ session_id: session.session_id, move: "<0-300>" }} />;
  if (v.phase === "reveal" && !(VIEWER_ID in v.bids)) return <McpCommandHint
    label="Your agent reveals its bid with" tool="reveal_move" args={{ session_id: session.session_id }} />;
  if (v.phase === "secrets" && !v.secrets.includes(VIEWER_ID)) return <McpCommandHint
    label="Your agent reveals its secret with" tool="submit_action"
    args={{ session_id: session.session_id, action_type: "reveal", payload: "{}" }} />;
  return null;
}
