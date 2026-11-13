import { BoardTable } from "./BoardTable";
import { plural } from "./boardKit";
import kit from "./BoardKit.module.css";
import styles from "./DiceTable.module.css";

export interface DiceBid { n: number; name: string; quantity: number; face: number }
/** One player's cup. `values` is the dice, or null while the cup is closed; a board only draws it when `over`. */
export interface DiceCup { id: string; name: string; values: number[] | null }

export interface DiceTableProps {
  bids: DiceBid[];
  /** The bid number that was called, and by whom; null until someone calls. */
  call: { bid: number; byName: string } | null;
  onesWild?: boolean;
  diceEach?: number;
  /** The game is finished and the record has revealed the dice. Without it no cup is ever drawn. */
  over: boolean;
  cups: DiceCup[];
  /** What the relay's result says the table held (count of the called face); drawn from the dice when absent. */
  showed?: number | null;
  /** The relay's verdict on the called bid: true held, false failed; null when not known. */
  held?: boolean | null;
}

const FACES = [1, 2, 3, 4, 5, 6];
const counts = (v: number, face: number, wild: boolean) => v === face || (wild && v === 1);

/**
 * Liar's dice: the bids as a ladder on a grid (quantity up, face across), each bid a marked cell with
 * its bidder, the called bid outlined. After the reveal: each cup as die faces, the dice that count for
 * the called face filled, and "needed 4, showed 3" as a bar against a mark. Cups stay closed until `over`.
 */
export function DiceTable({ bids, call, onesWild, diceEach, over, cups, showed, held }: DiceTableProps) {
  const top = Math.max(1, ...bids.map((b) => b.quantity));
  const rows = Array.from({ length: top }, (_, i) => top - i);
  const byCell = new Map(bids.map((b) => [`${b.quantity}:${b.face}`, b]));
  const called = call ? bids.find((b) => b.n === call.bid) : undefined;
  const open = over ? cups.filter((c) => c.values && c.values.length > 0) : [];
  const face = called?.face;
  const mine = (v: number) => face !== undefined && counts(v, face, !!onesWild);
  const computed = called && open.length ? open.reduce((n, c) => n + c.values!.filter(mine).length, 0) : null;
  const count = over && called ? (typeof showed === "number" ? showed : computed) : null;
  const scale = called && count !== null ? Math.max(called.quantity, count, 1) : 1;
  const verdict = called && count !== null ? (held ?? count >= called.quantity) : null;
  const caption = `Liar's dice: ${plural(bids.length, "bid")}${call ? `, bid ${call.bid} called` : ""}`;
  return (
    <div className={`${kit.board} ${styles.root}`}>
      <p className={kit.rules}>
        {diceEach ? `${diceEach} dice each, ` : ""}bids rise: more dice, or a higher face{onesWild ? " · ones are wild" : ""}
        {over ? "" : " · every cup stays closed until the game ends"}
      </p>
      {bids.length ? (
        <div className={kit.scroll}>
          <div className={styles.grid} aria-hidden>
            <div className={styles.row}>
              <span className={styles.qty} />
              {FACES.map((f) => <span key={f} className={styles.faceHead}>face {f}</span>)}
            </div>
            {rows.map((q) => (
              <div className={styles.row} key={q}>
                <span className={styles.qty}>{q}×</span>
                {FACES.map((f) => {
                  const b = byCell.get(`${q}:${f}`);
                  const isCalled = !!b && call?.bid === b.n;
                  return (
                    <span key={f} className={`${styles.cell}${b ? ` ${styles.bid}` : ""}${isCalled ? ` ${styles.called}` : ""}`}>
                      {b ? <>#{b.n} {b.name}{isCalled ? " ⚑" : ""}</> : null}
                    </span>
                  );
                })}
              </div>
            ))}
          </div>
        </div>
      ) : <p className={kit.foot}>No bid yet.</p>}
      {call ? <p className={kit.foot}>⚑ {call.byName} called liar on bid {call.bid}{called ? ` (${called.quantity} × ${called.face} by ${called.name})` : ""}</p> : null}

      {over && open.length ? (
        <div className={styles.cups} aria-hidden>
          {open.map((c) => (
            <div key={c.id} className={styles.cup}>
              <span className={styles.cupName}>{c.name}</span>
              <span className={styles.dice}>
                {c.values!.map((v, i) => (
                  <span key={i} className={`${styles.die}${mine(v) ? ` ${styles.counts}` : ""}`}>{v}</span>
                ))}
              </span>
            </div>
          ))}
        </div>
      ) : null}
      {over && called && count !== null ? (
        <div className={styles.meter} aria-hidden>
          <span className={styles.track}>
            <span className={styles.fill} style={{ width: `${(count / scale) * 100}%` }} />
            <span className={styles.need} style={{ left: `${(called.quantity / scale) * 100}%` }} />
          </span>
          <span className={styles.meterText}>needed {called.quantity}, showed {count}{verdict === null ? "" : verdict ? " — the bid held" : " — the bid failed"}</span>
        </div>
      ) : null}

      <BoardTable caption={caption} head={["bid", "by", "claims at least", "status"]}>
        {bids.map((b) => (
          <tr key={b.n}>
            <th scope="row">{b.n}</th><td>{b.name}</td><td>{b.quantity} × {b.face}</td>
            <td>{call?.bid === b.n ? `called liar by ${call.byName}` : ""}</td>
          </tr>
        ))}
        {!bids.length ? <tr><th scope="row">none</th><td /><td>no bid yet</td><td /></tr> : null}
        {cups.map((c) => (
          <tr key={`cup-${c.id}`}>
            <th scope="row">{c.name}&apos;s cup</th><td />
            <td>{over ? (c.values && c.values.length ? `held ${c.values.join(" ")}` : "never revealed") : "closed"}</td><td />
          </tr>
        ))}
        {over && called && count !== null ? (
          <tr><th scope="row">result</th><td /><td>needed {called.quantity}, showed {count}</td><td>{verdict ? "the bid held" : "the bid failed"}</td></tr>
        ) : null}
      </BoardTable>
    </div>
  );
}
