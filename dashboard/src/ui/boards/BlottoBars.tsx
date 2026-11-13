import type { CSSProperties } from "react";
import { BoardTable } from "./BoardTable";
import { STATE_WORD, plural, type BoardSeat } from "./boardKit";
import kit from "./BoardKit.module.css";
import styles from "./BlottoBars.module.css";

export interface BlottoSeat extends BoardSeat {
  /** The revealed allocation. Only read when `state` is "revealed"; a sealed seat has none. */
  alloc?: number[];
}

export interface BlottoBarsProps {
  /** The two players: the first grows its bars to the left of the centre line, the second to the right. */
  seats: BlottoSeat[];
  fields: number;
  troops: number;
  /** Dealt weights per field, or null for an even game. */
  weights?: number[] | null;
  /** Per field: who took it (a seat id), or null for a tie. Null overall while either side is unrevealed. */
  fieldWinners?: (string | null)[] | null;
  round?: number;
  rounds?: number;
  /** Rounds won so far, per seat id, for a game of several rounds. */
  roundWins?: Record<string, number>;
}

/**
 * Blotto as bars: a row per field, one bar per player growing from a centre line, the troop count at the
 * bar's end, a mark (◆) on the side that took the field, the field's weight when dealt. A sealed move is
 * a hatched placeholder with the word "sealed". Pure; the Arena and the Games panel draw the same thing.
 */
export function BlottoBars({ seats, fields, troops, weights, fieldWinners, round, rounds, roundWins }: BlottoBarsProps) {
  const rows = Array.from({ length: fields }, (_, i) => i);
  const [a, b] = seats;
  const allocOf = (s: BoardSeat & { alloc?: number[] } | undefined) => (s && s.state === "revealed" ? s.alloc : undefined);
  const aa = allocOf(a), bb = allocOf(b);
  // Bars use 80 % of a half at most; the rest holds the number and the mark at the bar's end.
  const scale = Math.max(troops / Math.max(1, fields), ...(aa ?? [0]), ...(bb ?? [0]), 1) * 1.25;
  const weight = (f: number) => weights?.[f] ?? 1;
  const winners = fieldWinners && fieldWinners.length === fields ? fieldWinners : null;
  const points = (s: BoardSeat | undefined) => winners && s ? rows.reduce((n, f) => n + (winners[f] === s.id ? weight(f) : 0), 0) : null;
  const unit = weights ? "points" : "fields";

  const side = (s: BlottoSeat | undefined, alloc: number[] | undefined, f: number, left: boolean) => {
    if (!s) return <div className={styles.cell} />;
    const took = winners?.[f] === s.id;
    const value = alloc?.[f];
    return (
      <div className={`${styles.cell} ${left ? styles.left : styles.right}`}>
        {alloc === undefined ? (
          s.state === "sealed"
            ? <span className={`${styles.full} ${kit.hatch}`} />
            : <span className={styles.none}>{s.state === "out" ? "×" : "·"}</span>
        ) : (
          <span className={styles.wrap} style={{ width: `${(100 * (value ?? 0)) / scale}%` } as CSSProperties}>
            <span className={`${styles.bar} ${left ? styles.barA : styles.barB}${took ? ` ${styles.took}` : ""}`} />
            <span className={styles.end}>
              <span className={styles.num}>{value}</span>
              {took ? <span className={styles.mark}>◆</span> : null}
            </span>
          </span>
        )}
      </div>
    );
  };

  const caption = `Blotto, ${plural(fields, "field")}, ${troops} troops each${weights ? ", field weights dealt" : ""}`;
  return (
    <div className={`${kit.board} ${styles.root}`}>
      <p className={kit.rules}>
        {plural(fields, "field")} · {troops} troops each{rounds && rounds > 1 ? ` · round ${Math.min(round ?? 1, rounds)} of ${rounds}` : ""}
        {weights ? " · each field has a weight (dealt)" : ""}
        {" "}· more troops takes a field, {weights ? "more weight" : "more fields"} takes the round
      </p>
      <div className={kit.scroll}>
        <div className={styles.grid} aria-hidden>
          <div className={styles.head}>
            <span className={styles.who}>{a?.winner ? "★ " : ""}{a?.name}</span>
            <span />
            <span className={`${styles.who} ${styles.whoB}`}>{b?.winner ? "★ " : ""}{b?.name}</span>
          </div>
          {rows.map((f) => (
            <div className={styles.row} key={f}>
              {side(a, aa, f, true)}
              <span className={styles.centre}>
                F{f + 1}{weights ? <span className={styles.weight}> ×{weight(f)}</span> : null}
                {winners && winners[f] === null && aa && bb ? <span className={styles.tie}> =</span> : null}
              </span>
              {side(b, bb, f, false)}
            </div>
          ))}
          <div className={styles.head}>
            <span className={styles.state}>{a ? STATE_WORD[a.state] : ""}{a?.note ? ` · ${a.note}` : ""}</span>
            <span />
            <span className={`${styles.state} ${styles.whoB}`}>{b ? STATE_WORD[b.state] : ""}{b?.note ? ` · ${b.note}` : ""}</span>
          </div>
        </div>
      </div>
      {winners ? (
        <p className={kit.foot}>
          {seats.map((s) => `${s.name} ${points(s)}`).join(" · ")} {unit} this round
          {roundWins && rounds && rounds > 1 ? ` · rounds won: ${seats.map((s) => `${s.name} ${roundWins[s.id] ?? 0}`).join(" · ")}` : ""}
        </p>
      ) : null}

      <BoardTable caption={caption} head={["field", ...(weights ? ["weight"] : []), ...seats.map((s) => s.name), "taken by"]}>
        {rows.map((f) => (
          <tr key={f}>
            <th scope="row">Field {f + 1}</th>
            {weights ? <td>{weight(f)}</td> : null}
            {seats.map((s) => {
              const al = allocOf(s);
              return <td key={s.id}>{al ? al[f] : STATE_WORD[s.state]}</td>;
            })}
            <td>{winners ? (winners[f] === null ? "tie" : seats.find((s) => s.id === winners[f])?.name ?? "") : "not yet known"}</td>
          </tr>
        ))}
      </BoardTable>
    </div>
  );
}
