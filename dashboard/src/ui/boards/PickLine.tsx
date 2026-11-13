import type { CSSProperties } from "react";
import { BoardTable } from "./BoardTable";
import { plural, type BoardPick } from "./boardKit";
import kit from "./BoardKit.module.css";
import styles from "./PickLine.module.css";

export interface PickLineProps {
  min: number;
  max: number;
  /** Revealed picks only. A hidden pick is passed as a count in `hidden`, never as a value. */
  picks: BoardPick[];
  hidden?: number;
  average: number | null;
  target: number | null;
  /** The table drew: the closest pick is shared. */
  draw?: boolean;
}

/**
 * Guess two-thirds: a number line with a pin for each revealed pick, a dashed mark for the average and
 * one for the target (two-thirds of it). Hidden picks are a count; the target appears once all are in.
 */
export function PickLine({ min, max, picks, hidden = 0, average, target, draw }: PickLineProps) {
  const at = (x: number) => `${((x - min) / (max - min)) * 100}%`;
  // Stack picks whose labels would overlap (about a quarter of the line on a phone) onto their own rows.
  const rows: number[] = [];
  const placed = [...picks].sort((a, b) => a.value - b.value).map((s) => {
    let row = 0;
    while (rows[row] !== undefined && s.value - rows[row] < 26) row++;
    rows[row] = s.value;
    return { s, row };
  });
  const height = Math.max(1, rows.length);
  const label = `Picks on a line from ${min} to ${max}: ${picks.map((p) => `${p.name} ${p.value}`).join(", ") || "none revealed"}`
    + `${average !== null ? `; average ${average.toFixed(1)}` : ""}${target !== null ? `; target ${target.toFixed(1)}` : ""}`;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(min + f * (max - min)));
  return (
    <div className={`${kit.board} ${styles.root}`}>
      <p className={kit.rules}>
        pick {min}–{max} · the pick closest to ⅔ of the average wins
        {hidden ? ` · ${plural(hidden, "pick")} still hidden — the target appears when all are revealed` : ""}
      </p>
      <div className={styles.line} style={{ "--rows": height } as CSSProperties} role="img" aria-label={label}>
        <div className={styles.axis} />
        {ticks.map((t) => (
          <span key={t} className={styles.tick} style={{ left: at(t) }}><span>{t}</span></span>
        ))}
        {average !== null ? (
          <span className={`${styles.mark} ${styles.avg}`} style={{ left: at(average) }}><span>avg {average.toFixed(1)}</span></span>
        ) : null}
        {target !== null ? (
          <span className={`${styles.mark} ${styles.target}${target < 15 ? ` ${styles.low}` : ""}`} style={{ left: at(target) }}>
            <span>target {target.toFixed(1)}</span>
          </span>
        ) : null}
        {placed.map(({ s, row }) => (
          <span key={s.id} className={`${styles.pick}${s.winner ? ` ${styles.winner}` : ""}`}
            style={{ left: at(s.value), "--row": row } as CSSProperties}>
            <span className={styles.pin} />
            <span className={styles.label}>{s.winner ? "★ " : ""}{s.name} {s.value}</span>
          </span>
        ))}
      </div>
      {draw ? <p className={kit.foot}>= the closest pick is shared: a draw</p> : null}
      <BoardTable caption="Guess two-thirds: the picks" head={["player", "pick"]}>
        {picks.map((p) => <tr key={p.id}><th scope="row">{p.name}{p.winner ? " (winner)" : ""}</th><td>{p.value}</td></tr>)}
        {hidden ? <tr><th scope="row">hidden</th><td>{plural(hidden, "pick")} not revealed</td></tr> : null}
        <tr><th scope="row">average</th><td>{average !== null ? average.toFixed(1) : "not known yet"}</td></tr>
        <tr><th scope="row">target</th><td>{target !== null ? target.toFixed(1) : "not known yet"}</td></tr>
      </BoardTable>
    </div>
  );
}
