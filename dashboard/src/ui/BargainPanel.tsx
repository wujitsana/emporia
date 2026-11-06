/**
 * Bargain (`emporia:bargain:v1`, docs/proposals/gamify-bargain.md §9) as a viewer
 * sees it: the public pool, the offers in order with any stated values marked
 * unchecked, whose turn and the clock; after the end, both real hands beside what
 * each side said, the split and both scores.
 *
 * Visibility only: the dashboard never offers or accepts. A player's next move is
 * shown as the MCP call to copy. While it runs the hands are sealed — a viewer who
 * is a player sees its own values (`private`, served to it alone), nobody else's.
 */
import Text from "@kit/Text";
import type { CSSProperties } from "react";
import { api } from "../api";
import type { Session } from "../api";
import { GOODS, bargainView, type BargainView, type Goods } from "../bargain";
import { useInterval } from "../hooks";
import { VIEWER_ID } from "../relayEnv";
import { whenText } from "../relayFormat";
import { clockText } from "../setPlay";
import { AuditBadge } from "./AuditBadge";
import { GameChat } from "./GameChat";
import { McpCommandHint } from "./McpCommandHint";
import { VerifyBadge } from "./VerifyBadge";

const num: CSSProperties = { fontVariantNumeric: "tabular-nums" };


const goodsText = (g: Goods | undefined) => GOODS.map((k) => `${g?.[k] ?? 0} ${k}`).join(" · ");

export function BargainPanel({ session: initial }: { session: Session }) {
  const id = initial.session_id;
  const running = initial.status === "active" || initial.status === "waiting";
  const [fresh] = useInterval(() => api.session(id), running ? 4_000 : 3_600_000, [id, running]);
  const [acts] = useInterval(() => api.sessionActions(id), running ? 4_000 : 3_600_000, [id, running]);
  const session = fresh ?? initial;
  const v = bargainView(session, acts?.actions);
  const live = session.status === "active" || session.status === "waiting";
  const [a, b] = session.participants;
  const over = !live;
  const left = Math.max(0, v.maxOffers - v.offers.length);
  const clock = clockText(v.secondsLeft);

  return (
    <div className="e-setplay e-bargain">
      <header className="e-setplay__head">
        <div>
          <p className="e-setplay__game">bargain</p>
          <p className="e-setplay__players">{session.participants.join(" v ")}</p>
        </div>
        <span className={`e-setplay__status${live ? " is-live" : ""}`}>
          {live ? (session.status === "waiting" ? "waiting" : "live") : "finished"}
        </span>
      </header>

      <p className="e-faint e-blotto__rules" style={num}>
        pool {goodsText(v.pool)} · worth {v.total} to each player, by priorities only it knows · {v.maxOffers} offers at most
        {" "}· no deal scores {v.walkAway} each
      </p>

      {over ? (
        <p className={`e-setplay__verdict${v.result?.winner ? " is-win" : ""}`} role="status">
          {v.ended?.how === "accepted" ? "deal" : v.ended?.how === "declined" ? "no deal" : v.result?.termination === "timeout" ? "on time" : "over"}
          {v.result?.winner ? <> — <span aria-hidden>★ </span>{v.result.winner} scores more</> : v.result ? " — a draw" : null}
          {v.result?.points ? (
            <span className="e-setplay__why" style={num}>
              {" "}— {Object.entries(v.result.points).map(([p, n]) => `${p} ${n}`).join(" · ")}
            </span>
          ) : v.result?.reason ? <span className="e-setplay__why"> — {v.result.reason}</span> : null}
        </p>
      ) : (
        <>
          {/* P9: the clock line is not a live region (it changes on every poll); only a change
              of turn is announced. */}
          <p className="e-setplay__wait">
            {session.status === "waiting" ? "Waiting for the second player."
              : `${session.current_agent}'s turn${clock ? ` · ${clock}` : ""} · ${left} offer${left === 1 ? "" : "s"} left${left === 0 ? " — accept or decline the last" : ""}.`}
          </p>
          <span className="e-sr" role="status">
            {session.status === "waiting" ? "Waiting for the second player." : `${session.current_agent}'s turn.`}
          </span>
        </>
      )}

      {v.offers.length === 0 ? (
        <Text className="e-note">No offer yet. The creator opens.</Text>
      ) : (
        <div className="e-blotto__scroll">
          <table className="e-blotto__grid e-bargain__offers">
            <thead>
              <tr>
                <th scope="col">#</th><th scope="col">by</th>
                <th scope="col">{a ?? "—"} gets</th><th scope="col">{b ?? "—"} gets</th>
                <th scope="col">stated values</th>
              </tr>
            </thead>
            <tbody>
              {v.offers.map((o) => {
                const taken = v.ended?.how === "accepted" && v.ended.offer === o.n;
                return (
                  <tr key={o.n} className={taken ? "is-winner" : undefined}>
                    <td style={num}>{o.n}</td>
                    <th scope="row">{o.by}</th>
                    <td style={num}>{goodsText(o.allocation[a])}</td>
                    <td style={num}>{goodsText(o.allocation[b])}</td>
                    <td style={num}>{o.claim ? <>{goodsText(o.claim)} <span className="e-faint">· stated, unchecked</span></> : <span className="e-faint">—</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <Hands v={v} participants={session.participants} over={over} />

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

/** Sealed while it runs; after the end each real hand beside the last thing its player said. */
function Hands({ v, participants, over }: { v: BargainView; participants: string[]; over: boolean }) {
  const revealed = over && Object.keys(v.hands).length > 0;
  return (
    <div className="e-seats" role="list" aria-label="Priorities">
      {participants.map((p) => {
        const claim = v.result?.claims?.[p];
        return (
          <div role="listitem" key={p} className="e-seat e-bargain__hand">
            <span className="e-seat__name">{p}{p === VIEWER_ID ? " (you)" : ""}</span>
            <span className="e-seat__state" style={num}>
              {revealed ? <>held {goodsText(v.hands[p])}</>
                : p === VIEWER_ID && v.mine ? <>your values {goodsText(v.mine)} <span className="e-faint">· only you see these</span></>
                : "▒▒ sealed until the end"}
              {revealed && claim ? (
                <span className="e-faint"> · said {goodsText(claim.claimed)} — {claim.matches ? "matched" : "differed"}</span>
              ) : null}
            </span>
            <span className="e-seat__move" style={num}>{v.result?.points?.[p] ?? ""}</span>
          </div>
        );
      })}
    </div>
  );
}

function NextMoveHint({ session, v }: { session: Session; v: BargainView }) {
  if (!VIEWER_ID || session.status !== "active" || session.current_agent !== VIEWER_ID) return null;
  const last = v.offers.length >= v.maxOffers;
  return (
    <McpCommandHint label={last ? "Your agent's move — the last offer stands; it answers with"
      : "Your agent's move — it offers (or accepts the offer on the table) with"}
      tool="submit_action" args={{ session_id: session.session_id, action_type: last ? "accept | decline" : "offer",
        payload: last ? "{}" : '{"keep": {"silk": 2, "spice": 0, "amber": 1}}' }} />
  );
}
