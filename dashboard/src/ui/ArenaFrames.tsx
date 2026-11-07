import Text from "@kit/Text";
import type { Frame, Slot } from "../arena";
import { playerName } from "../arena";
import type { BargainOffer, Goods } from "../bargain";
import { GOODS } from "../bargain";

type Book = Record<string, string> | undefined;

const goods = (g: Goods | undefined) =>
  GOODS.map((k) => `${g?.[k] ?? 0} ${k}`).join(" · ");

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
      {r.winner ? `Winner: ${playerName(r.winner, book, 22)}` : `No winner${r.termination ? ` (${r.termination.replace(/_/g, " ")})` : ""}`}
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
        <Text className="e-dim">
          {frame.rounds > 1 ? `Round ${frame.round} of ${frame.rounds} · ` : ""}{frame.phase} phase
        </Text>
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
        {frame.verdict ? <p className="e-arena-verdict" role="status">{frame.verdict.why}</p> : null}
      </div>
    );
  }
  if (frame.kind === "dice") {
    return (
      <div className="e-arena-frame" data-kind="dice">
        {frame.bids.length === 0 ? <Text className="e-faint">No bid yet.</Text> : (
          <ol className="e-arena-bids">
            {frame.bids.map((b) => (
              <li key={b.n} className={frame.call?.bid === b.n ? "is-called" : undefined}>
                <span className="e-arena-slot__who">{playerName(b.by, book)}</span>
                <span className="e-arena-move">{b.quantity} × {b.face}</span>
                {frame.call?.bid === b.n ? <span className="e-arena-call">called liar by {playerName(frame.call.by, book, 12)}</span> : null}
              </li>
            ))}
          </ol>
        )}
        {frame.call ? <Text className="e-faint" style={{ fontSize: 12 }}>secrets revealed: {frame.revealed.length}</Text> : null}
        {frame.dice ? (
          <ul className="e-arena-slots" aria-label="Dice, revealed with the record">
            {Object.entries(frame.dice).map(([p, d]) => (
              <li key={p} className="e-arena-slot is-revealed">
                <span className="e-arena-slot__who">{playerName(p, book)}</span>
                <span className="e-arena-move">{d.join(" ")}</span>
              </li>
            ))}
          </ul>
        ) : frame.call ? <Text className="e-faint" style={{ fontSize: 12 }}>dice stay hidden until the record reveals them</Text> : null}
        <Result r={frame.result} book={book} />
      </div>
    );
  }
  if (frame.kind === "auction") {
    return (
      <div className="e-arena-frame" data-kind="auction">
        <Slots slots={frame.slots} book={book} />
        {frame.secrets.length ? <Text className="e-faint" style={{ fontSize: 12 }}>secrets revealed: {frame.secrets.length}</Text> : null}
        {frame.value !== null ? (
          <p className="e-arena-verdict">
            V = {frame.value}{frame.base !== null ? ` (base ${frame.base})` : ""}
            {frame.signals ? ` · signals ${Object.entries(frame.signals).map(([p, v]) => `${playerName(p, book, 10)} ${v}`).join(", ")}` : ""}
          </p>
        ) : <Text className="e-faint" style={{ fontSize: 12 }}>V stays unknown until the secrets are revealed</Text>}
        <Result r={frame.result} book={book} />
      </div>
    );
  }
  if (frame.kind === "bargain") {
    return (
      <div className="e-arena-frame" data-kind="bargain">
        {frame.offers.length === 0 ? <Text className="e-faint">No offer yet.</Text> : (
          <ol className="e-arena-bids">
            {frame.offers.map((o: BargainOffer) => (
              <li key={o.n}>
                <span className="e-arena-slot__who">{playerName(o.by, book)}</span>
                <span className="e-arena-move">
                  {Object.entries(o.allocation).map(([p, g]) => `${playerName(p, book, 10)} ${goods(g)}`).join("  |  ")}
                </span>
                {o.claim ? <span className="e-faint"> claims {goods(o.claim)}</span> : null}
              </li>
            ))}
          </ol>
        )}
        {frame.ended ? <p className="e-arena-verdict">{playerName(frame.ended.by, book)} {frame.ended.how} offer {frame.offers.length}.</p> : null}
        {frame.hands ? (
          <Text className="e-faint" style={{ fontSize: 12, display: "block" }}>
            hands: {Object.entries(frame.hands).map(([p, g]) => `${playerName(p, book, 10)} ${goods(g)}`).join("  |  ")}
          </Text>
        ) : null}
        <Result r={frame.result} book={book} />
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
