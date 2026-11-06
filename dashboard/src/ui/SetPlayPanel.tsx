/**
 * A set play (blotto, guess ⅔, lowest unique) as it happens: the phase strip
 * (commit → reveal → winner), each seat's state, and the game's own board —
 * blotto's fields, the ⅔ target line, lowest unique's numbers. The same view
 * live and finished: a finished game is the strip at its last step.
 *
 * Visibility only. A player's next move is shown as the MCP call to copy.
 */
import Text from "@kit/Text";
import type { CSSProperties } from "react";
import { api } from "../api";
import type { Session } from "../api";
import { useInterval } from "../hooks";
import { VIEWER_ID } from "../relayEnv";
import { whenText } from "../relayFormat";
import {
  SET_PLAY_NAMES, callText, clockText, setPlayView,
  type Seat, type SetPlayView,
} from "../setPlay";
import { AuditBadge } from "./AuditBadge";
import { GameChat } from "./GameChat";
import { McpCommandHint } from "./McpCommandHint";
import { SessionMoney } from "./SessionMoney";
import { VerifyBadge } from "./VerifyBadge";

const num: CSSProperties = { fontVariantNumeric: "tabular-nums" };

export function SetPlayPanel({ session: initial }: { session: Session }) {
  const id = initial.session_id;
  const running = initial.status === "active" || initial.status === "waiting";
  // Follow a live table every few seconds; a finished one needs a single read.
  const [fresh] = useInterval(() => api.session(id), running ? 4_000 : 3_600_000, [id, running]);
  const [acts] = useInterval(() => api.sessionActions(id), running ? 4_000 : 3_600_000, [id, running]);
  const session = fresh ?? initial;
  const v = setPlayView(session, acts?.actions);
  const live = session.status === "active" || session.status === "waiting";

  return (
    <div className="e-setplay">
      <header className="e-setplay__head">
        <div>
          <p className="e-setplay__game">{SET_PLAY_NAMES[v.moduleType] ?? v.moduleType}</p>
          <p className="e-setplay__players">{session.participants.join(" v ")}</p>
        </div>
        <span className={`e-setplay__status${live ? " is-live" : ""}`}>
          {live ? (v.phase === "waiting" ? "waiting" : "live") : "finished"}
        </span>
      </header>

      <PhaseStrip v={v} />

      {v.verdict ? (
        <p className={`e-setplay__verdict${v.verdict.winner ? " is-win" : ""}`} role="status">
          {v.verdict.winner ? <><span aria-hidden>★ </span>{v.verdict.winner} wins</> : "draw"}
          <span className="e-setplay__why"> — {v.verdict.why}</span>
        </p>
      ) : (
        <p className="e-setplay__wait" role="status">{waitLine(v)}</p>
      )}

      {v.kind === "blotto" ? <BlottoBoard v={v} /> : <PickBoard v={v} />}

      <Seats v={v} />

      <div className="e-setplay__meta">
        <SessionMoney session={session} />
        <Text className="e-faint" style={{ fontSize: 11 }}>
          …{id.slice(-10)}{session.created_at ? ` · started ${whenText(session.created_at)}` : ""}
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

function waitLine(v: SetPlayView): string {
  const clock = clockText(v.secondsLeft);
  const on = v.pending.length ? `waiting on ${v.pending.join(", ")}` : "";
  if (v.phase === "waiting") return "Waiting for players to join. The game starts when the table is full.";
  const tail = `${on ? ` — ${on}` : ""}${clock ? ` · ${clock}` : ""}.`;
  if (v.phase === "commit")
    return `Moves are hidden: each player sends only a hash of its move${tail}`;
  return `Every move is fixed, and now each is shown${tail} Not revealing in time loses.`;
}

const STEPS = [
  { id: "commit", label: "commit" },
  { id: "reveal", label: "reveal" },
  { id: "done", label: "winner" },
] as const;

function PhaseStrip({ v }: { v: SetPlayView }) {
  const at = v.phase === "waiting" ? -1 : STEPS.findIndex((s) => s.id === v.phase);
  return (
    // Divs, not a list: the kit numbers and indents list items.
    <div className="e-phase-strip" role="list" aria-label="Phase">
      {STEPS.map((s, i) => {
        const state = i < at ? "done" : i === at ? "now" : "next";
        return (
          <span role="listitem" key={s.id} className={`e-phase-strip__step is-${state}`} aria-current={state === "now" ? "step" : undefined}>
            <span className="e-phase-strip__n" aria-hidden>{state === "done" || (s.id === "done" && state === "now") ? "✓" : i + 1}</span>
            <span>{s.label}</span>
            {s.id === "done" && v.verdict?.winner ? <span className="e-phase-strip__who">{v.verdict.winner}</span> : null}
          </span>
        );
      })}
    </div>
  );
}

/**
 * Calls (`:v2` set plays): a forecast sealed with the first move. While hidden the
 * table shows only that one was sealed; once revealed, the percent beside the move.
 */
function CallTag({ v, agent }: { v: SetPlayView; agent: string }) {
  const t = callText(v, agent);
  if (!t) return null;
  return <span className={`e-call-tag${typeof v.calls[agent] === "number" ? " is-revealed" : ""}`}
    title="a call: its own chance of winning, sealed with its first move and scored as Insight">{t}</span>;
}

const SEAT_TEXT: Record<Seat["state"], string> = {
  to_commit: "· to commit",
  committed: "■ committed",
  revealed: "◆ revealed",
  out: "× out — did not reveal",
};

function Seats({ v }: { v: SetPlayView }) {
  // Blotto's seats are the rows of its board; the pick games list them here.
  if (v.kind === "blotto") return null;
  return (
    <div className="e-seats" role="list">
      {v.seats.map((s) => (
        <div role="listitem" key={s.agent} className={`e-seat is-${s.state}${s.winner ? " is-winner" : ""}`}>
          <span className="e-seat__name">{s.winner ? "★ " : ""}{s.agent}{s.agent === VIEWER_ID ? " (you)" : ""}</span>
          <span className="e-seat__state">{SEAT_TEXT[s.state]}{s.hash ? <span className="e-faint"> {s.hash}…</span> : null}
            <CallTag v={v} agent={s.agent} /></span>
          <span className="e-seat__move" style={num}>{s.move !== undefined ? String(s.move) : s.state === "committed" ? "▒▒" : ""}</span>
        </div>
      ))}
    </div>
  );
}

function BlottoBoard({ v }: { v: SetPlayView }) {
  const b = v.blotto!;
  const fields = Array.from({ length: b.battlefields }, (_, i) => i);
  const last = v.phase === "done" ? b.rounds_done[b.rounds_done.length - 1] : undefined;
  const showWon = v.phase === "done" || b.rounds > 1;
  return (
    <div className="e-blotto">
      <p className="e-faint e-blotto__rules" style={num}>
        {b.battlefields} fields · {b.troops} troops each · round {Math.min(b.round, b.rounds)} of {b.rounds}
        {" "}· more troops takes a field, more fields takes the round
      </p>
      <div className="e-blotto__scroll">
        <table className="e-blotto__grid">
          <thead>
            <tr>
              <th scope="col"><span className="e-sr">player</span></th>
              {fields.map((f) => <th key={f} scope="col">F{f + 1}</th>)}
              {showWon && <th scope="col">{b.rounds > 1 ? "rounds" : "fields"}</th>}
            </tr>
          </thead>
          <tbody>
            {v.seats.map((s) => {
              const alloc = Array.isArray(s.move) ? s.move : null;
              const took = last ? last.fieldWinners.filter((w) => w === s.agent).length : null;
              return (
                <tr key={s.agent} className={s.winner ? "is-winner" : undefined}>
                  <th scope="row">
                    <span className="e-blotto__name">{s.winner ? "★ " : ""}{s.agent}</span>
                    <span className="e-blotto__state">{SEAT_TEXT[s.state]}<CallTag v={v} agent={s.agent} /></span>
                  </th>
                  {fields.map((f) => {
                    const won = last?.fieldWinners[f] === s.agent;
                    return (
                      <td key={f} className={`${won ? "is-won" : ""}${alloc ? "" : " is-hidden"}`} style={num}
                        title={alloc ? undefined : s.state === "committed" ? `committed: ${s.hash}…` : "no move yet"}>
                        {alloc ? alloc[f] : s.state === "committed" ? "▒▒" : "·"}
                      </td>
                    );
                  })}
                  {showWon && <td style={num} className="e-blotto__took">{b.rounds > 1 ? b.wins[s.agent] ?? 0 : took}</td>}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {b.rounds_done.length > (v.phase === "done" ? 1 : 0) ? (
        <details className="e-blotto__history">
          <summary>earlier rounds ({v.phase === "done" ? b.rounds_done.length - 1 : b.rounds_done.length})</summary>
          {(v.phase === "done" ? b.rounds_done.slice(0, -1) : b.rounds_done).map((r) => (
            <Text key={r.round} className="e-faint" style={{ ...num, fontSize: 12, display: "block" }}>
              round {r.round}: {Object.entries(r.allocations).map(([p, a]) => `${p} [${a.join(" ")}]`).join(" · ")}
              {r.winner ? ` → ${r.winner}` : " → tied"}
            </Text>
          ))}
        </details>
      ) : null}
    </div>
  );
}

function PickBoard({ v }: { v: SetPlayView }) {
  const p = v.pick!;
  const revealed = v.seats.filter((s) => typeof s.move === "number") as (Seat & { move: number })[];
  const hidden = v.seats.filter((s) => s.state === "committed").length;
  if (v.kind === "two-thirds") return <TwoThirdsLine v={v} revealed={revealed} hidden={hidden} />;

  // Lowest unique: a box per number, its pickers inside.
  const numbers = Array.from({ length: p.max - p.min + 1 }, (_, i) => p.min + i);
  const by = new Map<number, string[]>();
  for (const s of revealed) by.set(s.move, [...(by.get(s.move) ?? []), s.agent]);
  return (
    <div className="e-lowest">
      <p className="e-faint e-lowest__rules">
        pick {p.min}–{p.max} · the lowest number only one player picked wins
        {hidden ? ` · ${hidden} pick${hidden === 1 ? "" : "s"} still hidden` : ""}
      </p>
      <div className="e-lowest__row" role="list">
        {numbers.map((n) => {
          const who = by.get(n) ?? [];
          const winning = v.verdict?.winner != null && who.length === 1 && who[0] === v.verdict.winner;
          const cls = who.length === 0 ? "is-empty" : who.length === 1 ? "is-unique" : "is-taken";
          return (
            <div role="listitem" key={n} className={`e-lowest__cell ${cls}${winning ? " is-winner" : ""}`}>
              <span className="e-lowest__n" style={num}>{n}</span>
              {who.map((a) => <span key={a} className="e-lowest__who">{winning ? "★ " : ""}{a}</span>)}
              {who.length > 1 ? <span className="e-lowest__tag">×{who.length}</span> : who.length === 1 ? <span className="e-lowest__tag">once</span> : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function TwoThirdsLine({ v, revealed, hidden }: { v: SetPlayView; revealed: (Seat & { move: number })[]; hidden: number }) {
  const { min, max, average, target } = v.pick!;
  const at = (x: number) => `${((x - min) / (max - min)) * 100}%`;
  // Stack picks whose labels would overlap (about a quarter of the line on a phone) onto their own rows.
  const rows: number[] = [];
  const placed = [...revealed].sort((a, b) => a.move - b.move).map((s) => {
    let row = 0;
    while (rows[row] !== undefined && s.move - rows[row] < 26) row++;
    rows[row] = s.move;
    return { s, row };
  });
  const height = Math.max(1, rows.length);
  return (
    <div className="e-twothirds">
      <p className="e-faint e-twothirds__rules">
        pick 0–100 · the pick closest to ⅔ of the average wins
        {hidden ? ` · ${hidden} pick${hidden === 1 ? "" : "s"} still hidden — the target appears when all are revealed` : ""}
      </p>
      <div className="e-numline" style={{ "--rows": height } as CSSProperties} role="img"
        aria-label={`picks: ${revealed.map((s) => `${s.agent} ${s.move}`).join(", ") || "none revealed"}${target !== null ? `; target ${target.toFixed(1)}` : ""}`}>
        <div className="e-numline__axis" />
        {[0, 25, 50, 75, 100].map((t) => (
          <span key={t} className="e-numline__tick" style={{ left: at(t) }}><span>{t}</span></span>
        ))}
        {average !== null ? (
          <span className="e-numline__mark e-numline__mark--avg" style={{ left: at(average) }}>
            <span>avg {average.toFixed(1)}</span>
          </span>
        ) : null}
        {target !== null ? (
          <span className={`e-numline__mark e-numline__mark--target${target < 15 ? " is-low" : ""}`} style={{ left: at(target) }}>
            <span>target {target.toFixed(1)}</span>
          </span>
        ) : null}
        {placed.map(({ s, row }) => (
          <span key={s.agent} className={`e-numline__pick${s.winner ? " is-winner" : ""}`}
            style={{ left: at(s.move), "--row": row } as CSSProperties}>
            <span className="e-numline__pin" aria-hidden />
            <span className="e-numline__label">{s.winner ? "★ " : ""}{s.agent} {s.move}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

/** For a player whose move the table waits on: the call that makes it. Never a button. */
function NextMoveHint({ session, v }: { session: Session; v: SetPlayView }) {
  if (!VIEWER_ID || !v.pending.includes(VIEWER_ID)) return null;
  const me = v.seats.find((s) => s.agent === VIEWER_ID);
  if (v.phase === "commit" && me?.state === "to_commit") {
    const example = v.kind === "blotto"
      ? `[${Array.from({ length: v.blotto!.battlefields }, () => Math.floor(v.blotto!.troops / v.blotto!.battlefields)).join(", ")}]`
      : v.kind === "two-thirds" ? "22" : "3";
    return (
      <McpCommandHint label="Your agent's move — it commits a hidden move with"
        tool="commit_move" args={{ session_id: session.session_id, move: example, rationale: "<why this move — 15 chars min>",
          ...(v.moduleType.endsWith(":v2") && (v.blotto?.round ?? 1) === 1 ? { call: "<optional: its chance of winning, 0.01–0.99>" } : {}) }} />
    );
  }
  if (v.phase === "reveal" && me?.state === "committed") {
    return (
      <McpCommandHint label="Your agent's move — it reveals what it committed with"
        tool="reveal_move" args={{ session_id: session.session_id, rationale: "<why — 15 chars min>" }} />
    );
  }
  return null;
}
