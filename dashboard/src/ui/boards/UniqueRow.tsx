import { BoardTable } from "./BoardTable";
import { plural, type BoardPick } from "./boardKit";
import kit from "./BoardKit.module.css";
import styles from "./UniqueRow.module.css";

export interface UniqueRowProps {
  min: number;
  max: number;
  /** Revealed picks only; a hidden pick is a count in `hidden`. */
  picks: BoardPick[];
  hidden?: number;
  /** The table is over and nobody picked a number alone. */
  draw?: boolean;
}

/**
 * Lowest unique: a cell per number with its pickers inside. A number picked once says "once", a number
 * picked by several is struck through and says "×n"; the winner's cell is filled and starred.
 */
export function UniqueRow({ min, max, picks, hidden = 0, draw }: UniqueRowProps) {
  const numbers = Array.from({ length: max - min + 1 }, (_, i) => min + i);
  const by = new Map<number, BoardPick[]>();
  for (const p of picks) by.set(p.value, [...(by.get(p.value) ?? []), p]);
  return (
    <div className={`${kit.board} ${styles.root}`}>
      <p className={kit.rules}>
        pick {min}–{max} · the lowest number only one player picked wins
        {hidden ? ` · ${plural(hidden, "pick")} still hidden` : ""}
      </p>
      <div className={styles.row} aria-hidden>
        {numbers.map((n) => {
          const who = by.get(n) ?? [];
          const winning = who.length === 1 && !!who[0].winner;
          const state = who.length === 0 ? styles.empty : who.length === 1 ? styles.unique : styles.taken;
          return (
            <div key={n} className={`${styles.cell} ${state}${winning ? ` ${styles.winner}` : ""}`}>
              <span className={styles.n}>{n}</span>
              {who.map((p) => <span key={p.id} className={styles.who}>{winning ? "★ " : ""}{p.name}</span>)}
              {who.length > 1 ? <span className={styles.tag}>×{who.length}</span> : who.length === 1 ? <span className={styles.tag}>once</span> : null}
            </div>
          );
        })}
      </div>
      {draw ? <p className={kit.foot}>no number was picked by only one player: a draw</p> : null}
      <BoardTable caption="Lowest unique: the picks" head={["number", "picked by", "times"]}>
        {numbers.filter((n) => by.has(n)).map((n) => {
          const who = by.get(n)!;
          return (
            <tr key={n}>
              <th scope="row">{n}</th>
              <td>{who.map((p) => p.name).join(", ")}{who.length === 1 && who[0].winner ? " (winner)" : ""}</td>
              <td>{who.length === 1 ? "once" : who.length}</td>
            </tr>
          );
        })}
        {hidden ? <tr><th scope="row">hidden</th><td>{plural(hidden, "pick")} not revealed</td><td /></tr> : null}
        {!picks.length && !hidden ? <tr><th scope="row">none</th><td>no pick yet</td><td /></tr> : null}
      </BoardTable>
    </div>
  );
}
