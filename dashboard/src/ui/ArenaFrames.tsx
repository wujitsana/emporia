import Text from "@kit/Text";
import type { Frame, Slot } from "../arena";
import { playerName } from "../arena";
import { FoldDetail } from "./FoldDetail";
import { SetPlayBoard } from "./boards/SetPlayBoard";
import { modelFromFrame } from "./boards/setPlayModel";
import { AuctionLine } from "./boards/AuctionLine";
import { BargainSplit } from "./boards/BargainSplit";
import { DiceTable } from "./boards/DiceTable";
import { SluiceBoard } from "./boards/SluiceBoard";
import { sluiceFromFrame } from "./boards/sluiceModel";
import { auctionFromFrame, bargainFromFrame, diceFromFrame } from "./boards/chanceModel";

type Book = Record<string, string> | undefined;

function moveText(m: number | number[] | undefined): string {
  if (m === undefined) return "";
  return Array.isArray(m) ? m.join(" · ") : String(m);
}

/** One seat of a commit-and-reveal table: waiting, sealed (never the move), or revealed. */
function SlotCell({ s }: { s: Slot }) {
  if (s.state === "revealed") return <span className="e-arena-move">{moveText(s.move)}</span>;
  if (s.state === "sealed") return <span className="e-arena-sealed" title="committed; hidden until its reveal step">▒▒ sealed</span>;
  return <span className="e-faint">waiting</span>;
}

function Slots({ slots, book }: { slots: Slot[]; book: Book }) {
  return (
    <ul className="e-arena-slots">
      {slots.map((s) => (
        <li key={s.agent} className={`e-arena-slot is-${s.state}`}>
          <span className="e-arena-slot__who">{playerName(s.agent, book)}</span>
          <SlotCell s={s} />
          {s.call ? <span className="e-faint e-arena-slot__call">{s.call}</span> : null}
        </li>
      ))}
    </ul>
  );
}

function Result({ r, book }: { book: Book; r: { winner: string | null; termination?: string; reason?: string; points?: Record<string, number> } | null }) {
  return null === r ? null : (
    <p className="e-arena-verdict" role="status">
      {r.termination === "deal" && r.points && Object.keys(r.points).length > 1
        // A Bargain deal both sides accepted: nobody lost; each side's own points (as on the Results row).
        ? `Deal: ${Object.entries(r.points).map(([p, n]) => `${playerName(p, book, 22)} ${n}`).join(" · ")} points`
        : r.winner ? `Winner: ${playerName(r.winner, book, 22)}` : `No winner${r.termination ? ` (${r.termination.replace(/_/g, " ")})` : ""}`}
      {r.reason ? ` — ${r.reason}` : ""}
    </p>
  );
}

/** The state of the match at one step, whatever the game. */
export function ArenaFrame({ frame, book }: { frame: Frame; book: Book }) {
  if (frame.kind === "blotto" || frame.kind === "two-thirds" || frame.kind === "lowest-unique") {
    const unit = frame.kind === "blotto" ? "fields" : "pick";
    return (
      <div className="e-arena-frame" data-kind={frame.kind}>
        {frame.verdict ? <p className="e-arena-verdict" role="status">{frame.verdict.why}</p> : null}
        <FoldDetail fold={!!frame.verdict} label="the moves, round by round">
        <Text className="e-dim">
          {frame.rounds > 1 ? `Round ${frame.round} of ${frame.rounds} · ` : ""}{frame.phase} phase
        </Text>
        <SetPlayBoard model={modelFromFrame(frame, (id) => playerName(id, book, 10))} />
        <Slots slots={frame.slots} book={book} />
        {frame.fields ? (
          <Text className="e-faint" style={{ fontSize: 12 }}>
            {unit} won: {frame.fields.map((w) => (w ? playerName(w, book, 10) : "tie")).join(" · ")}
          </Text>
        ) : null}
        {frame.past.length > 0 ? (
          <div className="e-arena-past">
            <Text className="e-dim" style={{ fontSize: 12 }}>Earlier rounds</Text>
            {frame.past.map((r) => (
              <Text key={r.round} className="e-faint" style={{ fontSize: 12, display: "block" }}>
                round {r.round}: {Object.entries(r.moves).map(([p, m]) => `${playerName(p, book, 10)} ${moveText(m)}`).join("  vs  ")}
              </Text>
            ))}
          </div>
        ) : null}
        </FoldDetail>
      </div>
    );
  }
  if (frame.kind === "dice") {
    return (
      <div className="e-arena-frame" data-kind="dice">
        <Result r={frame.result} book={book} />
        <FoldDetail fold={!!frame.result} label="the bids and the dice">
        <DiceTable {...diceFromFrame(frame, (id) => playerName(id, book, 10))} />
        {frame.call ? <Text className="e-faint" style={{ fontSize: 12 }}>secrets revealed: {frame.revealed.length}</Text> : null}
        {!frame.dice && frame.call ? <Text className="e-faint" style={{ fontSize: 12 }}>dice stay hidden until the record reveals them</Text> : null}
        </FoldDetail>
      </div>
    );
  }
  if (frame.kind === "auction") {
    return (
      <div className="e-arena-frame" data-kind="auction">
        <Result r={frame.result} book={book} />
        <FoldDetail fold={!!frame.result} label="the bids and the signals">
        <AuctionLine {...auctionFromFrame(frame, (id) => playerName(id, book, 10))} />
        {frame.secrets.length ? <Text className="e-faint" style={{ fontSize: 12 }}>secrets revealed: {frame.secrets.length}</Text> : null}
        {frame.value === null ? <Text className="e-faint" style={{ fontSize: 12 }}>V stays unknown until the secrets are revealed</Text> : null}
        </FoldDetail>
      </div>
    );
  }
  if (frame.kind === "bargain") {
    return (
      <div className="e-arena-frame" data-kind="bargain">
        <Result r={frame.result} book={book} />
        {frame.result && frame.ended ? <p className="e-arena-verdict">{playerName(frame.ended.by, book)} {frame.ended.how} offer {frame.offers.length}.</p> : null}
        <FoldDetail fold={!!frame.result} label={`the offers and the hands (${frame.offers.length})`}>
        <BargainSplit {...bargainFromFrame(frame, (id) => playerName(id, book, 10))} />
        {frame.ended && !frame.result ? <p className="e-arena-verdict">{playerName(frame.ended.by, book)} {frame.ended.how} offer {frame.offers.length}.</p> : null}
        </FoldDetail>
      </div>
    );
  }
  if (frame.kind === "sluice") {
    const props = sluiceFromFrame(frame, (id) => playerName(id, book, 10));
    return (
      <div className="e-arena-frame" data-kind="sluice">
        {props.verdict ? <p className="e-arena-verdict" role="status">{props.verdict}</p> : null}
        <FoldDetail fold={!!props.verdict} label="the board and the drops">
        <SluiceBoard {...props} />
        </FoldDetail>
      </div>
    );
  }
  if (frame.kind === "log") {
    return (
      <div className="e-arena-frame" data-kind="log">
        {frame.rows.length === 0 ? <Text className="e-faint">Nothing has happened yet.</Text> : frame.rows.map((r) => (
          <Text key={r.n} style={{ fontSize: 12, display: "block" }}>
            <span className="e-faint">{r.n}</span> <span className="e-dim">{playerName(r.agent, book, 12)}</span> {r.text}
          </Text>
        ))}
      </div>
    );
  }
  return null;
}
