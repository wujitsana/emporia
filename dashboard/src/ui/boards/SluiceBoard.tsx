import { BoardTable } from "./BoardTable";
import { plural } from "./boardKit";
import kit from "./BoardKit.module.css";
import styles from "./SluiceBoard.module.css";

export interface SluiceMoveRow { n: number; name: string; channel: number; row: number | null; gate: number | null }

export interface SluiceBoardProps {
  cols: number;
  rows: number;
  /** Seat A then seat B, already short display names. */
  seats: [string, string];
  /** False before the opening is dealt: no board is drawn. */
  dealt: boolean;
  nameRound: number | null;
  /** Rows top first; `.` empty, `#` silt, `A`, `B`. */
  board: string[][];
  moves: SluiceMoveRow[];
  /** The move number of the stone dropped last (marked), and its place. */
  last: { n: number; name: string; channel: number; row: number | null } | null;
  /** The shut channel and whether it is shut for the next drop (`now`) or was shut for the last one. */
  gate: { channel: number; now: boolean } | null;
  /** The winning four, `[channel, row]` each. */
  line: [number, number][] | null;
  threes: [number, number] | null;
  left: [number, number];
  /** One plain line for the verdict, when there is one. */
  verdict: string | null;
}

const same = (a: [number, number][] | null, c: number, r: number) => !!a?.some(([x, y]) => x === c && y === r);

/**
 * Sluice: six channels, five rows, a gate across the top of the channel the beacon shut, neutral silt, a
 * filled square for seat A and a hollow ring for seat B, the last drop marked, the winning four outlined
 * and, at the cap, the live threes. The drawing is `aria-hidden`; a table states every cell, another the moves.
 */
export function SluiceBoard(p: SluiceBoardProps) {
  const channels = Array.from({ length: p.cols }, (_, i) => i + 1);
  const rule = `${p.cols} channels, ${p.rows} rows · each turn the beacon shuts one channel · four of your stones in a line wins; silt and the other side's stones break a line · at the cap, more live threes wins, level: the player who moved second wins`;
  if (!p.dealt) {
    return (
      <div className={`${kit.board} ${styles.root}`}>
        <p className={kit.rules}>{rule}</p>
        <p className={kit.foot}>{p.nameRound !== null ? `The opening is dealt when drand round ${p.nameRound} is published.` : "The opening is dealt when the beacon's round is published."}</p>
      </div>
    );
  }
  const word = (m: string) => (m === "A" ? p.seats[0] : m === "B" ? p.seats[1] : m === "#" ? "silt" : "empty");
  const gateWhere = p.gate ? (p.gate.now ? `Channel ${p.gate.channel} is shut for the next drop.` : `Channel ${p.gate.channel} was shut for the last drop.`) : null;
  const caption = `Sluice: ${p.cols} channels by ${p.rows} rows, ${plural(p.moves.length, "drop")}${p.gate ? `, channel ${p.gate.channel} ${p.gate.now ? "shut now" : "was shut"}` : ""}`;
  return (
    <div className={`${kit.board} ${styles.root}`}>
      <p className={kit.rules}>{rule}</p>
      <div className={kit.scroll}>
        <div className={styles.grid} style={{ ["--cols" as string]: p.cols }} aria-hidden>
          <div className={styles.row}>
            <span />
            {channels.map((c) => (
              <span key={c} className={`${styles.head}${p.gate?.channel === c ? ` ${styles.gate}` : ""}`}>
                {c}
                {p.gate?.channel === c ? <span className={styles.gateWord}>shut</span> : null}
              </span>
            ))}
          </div>
          {p.board.map((cells, ri) => {
            const r = p.rows - ri;
            return (
              <div className={styles.row} key={ri}>
                <span className={styles.rowNo}>{r}</span>
                {cells.map((m, ci) => {
                  const c = ci + 1;
                  const n = p.moves.find((x) => x.channel === c && x.row === r)?.n;
                  const isLast = !!p.last && p.last.channel === c && p.last.row === r;
                  const cls = [styles.cell, m === "#" ? styles.silt : "", same(p.line, c, r) ? styles.win : "", isLast ? styles.last : "",
                    p.gate?.channel === c && m === "." ? styles.shut : ""].filter(Boolean).join(" ");
                  return (
                    <span key={c} className={cls}>
                      {m === "A" ? <span className={styles.stoneA} /> : m === "B" ? <span className={styles.stoneB} /> : null}
                      {n !== undefined && (m === "A" || m === "B") ? <span className={styles.n}>{n}</span> : null}
                    </span>
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>
      <p className={styles.legend} aria-hidden>
        <span className={styles.key}><span className={styles.keyA} /> {p.seats[0]}</span>
        <span className={styles.key}><span className={styles.keyB} /> {p.seats[1]}</span>
        <span className={styles.key}><span className={styles.keySilt} /> silt</span>
      </p>
      {gateWhere ? <p className={kit.foot}>{gateWhere}</p> : null}
      {p.last ? <p className={kit.foot}>Last drop: {p.last.name}, channel {p.last.channel}{p.last.row ? `, row ${p.last.row}` : ""} (move {p.last.n}).</p> : <p className={kit.foot}>No drop yet: the opening as the beacon dealt it.</p>}
      <p className={kit.foot}>Drops left: {p.seats[0]} {p.left[0]} · {p.seats[1]} {p.left[1]}</p>
      {p.line ? <p className={kit.foot}>Four in a line: {p.line.map(([c, r]) => `channel ${c} row ${r}`).join(", ")}.</p> : null}
      {p.threes ? <p className={kit.foot}>Live threes at the cap: {p.seats[0]} {p.threes[0]}, {p.seats[1]} {p.threes[1]}.</p> : null}
      {p.verdict ? <p className={kit.foot}><strong>{p.verdict}.</strong></p> : null}

      <BoardTable caption={caption} head={["row", ...channels.map((c) => `channel ${c}`)]}>
        {p.board.map((cells, ri) => (
          <tr key={ri}>
            <th scope="row">{p.rows - ri}</th>
            {cells.map((m, ci) => (
              <td key={ci}>{word(m)}{same(p.line, ci + 1, p.rows - ri) ? " (in the winning line)" : ""}</td>
            ))}
          </tr>
        ))}
      </BoardTable>
      <BoardTable caption="Sluice drops in order" head={["move", "by", "channel", "row", "channel shut"]}>
        {p.moves.map((m) => (
          <tr key={m.n}><th scope="row">{m.n}</th><td>{m.name}</td><td>{m.channel}</td><td>{m.row ?? ""}</td><td>{m.gate ?? "none"}</td></tr>
        ))}
        {!p.moves.length ? <tr><th scope="row">none</th><td /><td /><td /><td /></tr> : null}
      </BoardTable>
    </div>
  );
}
