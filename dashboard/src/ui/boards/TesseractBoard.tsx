import { AXES } from "../../tesseract";
import { BoardTable } from "./BoardTable";
import { plural } from "./boardKit";
import kit from "./BoardKit.module.css";
import styles from "./TesseractBoard.module.css";

export interface TesseractBoardProps {
  epoch: number;
  epochs: number;
  phase: "seal" | "open" | "secrets" | "done";
  seats: { name: string; state: "waiting" | "sealed" | "opened" | "out"; winner: boolean }[];
  /** The opening mix, W X Y Z in basis points; null before the opening is dealt. */
  opening: number[] | null;
  rounds: { epoch: number; mix: number[]; dark: number; pushes: { name: string; text: string }[]; silent: string[]; late: string[] }[];
  verdict: string | null;
  results: { place: number; name: string; total: number; index: number; target: string | null }[] | null;
}

const WORD = { waiting: "waiting", sealed: "sealed", opened: "opened", out: "out" } as const;
const PHASE = { seal: "sealing the push", open: "opening the pushes", secrets: "revealing the secrets", done: "over" } as const;
const pct = (bp: number) => (bp / 100).toFixed(1);

function Bars({ mix, label }: { mix: number[]; label: string }) {
  return (
    <div className={styles.bars} aria-hidden>
      <span className={styles.label}>{label}</span>
      {AXES.map((a, k) => (
        <span key={a} className={styles.bar}>
          <span className={styles.fill} style={{ width: `${Math.min(100, (mix[k] ?? 0) / 40)}%` }} />
          <span className={styles.value}>{a} {pct(mix[k] ?? 0)}</span>
        </span>
      ))}
    </div>
  );
}

/**
 * Tesseract, the table: who has sealed (never what), the mix of each round with who pushed what once opened, and,
 * when the table is over, the totals and the targets. Visibility only, theme variables only. The bars are
 * `aria-hidden`; a table states every number. The channel network itself (the cube) is the full view's, T2.
 */
export function TesseractBoard(p: TesseractBoardProps) {
  const rule = `Tesseract: ${p.epochs} rounds · each round everyone secretly widens or narrows up to four channels of a shared network, 8 units in all · the network's mix of W X Y Z is the share each output gets · your points are how much your pushes moved the mix toward your own private target`;
  const mixes = [...(p.opening ? [{ label: "opening", mix: p.opening }] : []), ...p.rounds.map((r) => ({ label: `round ${r.epoch}`, mix: r.mix }))];
  return (
    <div className={`${kit.board} ${styles.root}`}>
      <p className={kit.rules}>{rule}</p>
      <p className={kit.foot}>Round {p.epoch} of {p.epochs}: {PHASE[p.phase]}.</p>
      <ul className={styles.seats}>
        {p.seats.map((s) => (
          <li key={s.name} className={`${styles.seat}${s.state === "sealed" ? ` ${kit.hatch}` : ""}${s.winner ? ` ${styles.winner}` : ""}`}>
            <span>{s.name}</span> <span className="e-faint">{WORD[s.state]}{s.winner ? " · winner" : ""}</span>
          </li>
        ))}
      </ul>
      {mixes.length === 0 ? <p className={kit.foot}>The opening is dealt when the beacon's round is published.</p> : (
        <div className={kit.scroll}>
          {mixes.map((m, k) => {
            const r = p.rounds.find((x) => `round ${x.epoch}` === m.label);
            return (
              <div key={m.label} className={styles.round}>
                <Bars mix={m.mix} label={m.label} />
                {r ? (
                  <p className={styles.pushes}>
                    {r.pushes.length ? r.pushes.map((x) => `${x.name}: ${x.text}`).join(" · ") : "no one pushed"}
                    {r.dark ? ` · ${plural(r.dark, "channel")} dark` : ""}
                    {r.silent.length ? ` · sat out: ${r.silent.join(", ")}` : ""}
                    {r.late.length ? ` · put out: ${r.late.join(", ")}` : ""}
                  </p>
                ) : <span hidden>{k}</span>}
              </div>
            );
          })}
        </div>
      )}
      {p.verdict ? <p className={kit.foot} role="status"><strong>{p.verdict}</strong></p> : null}
      {p.results ? (
        <ol className={styles.results}>
          {p.results.map((r) => (
            <li key={r.name}>{r.name}: {r.total} points · table index {r.index}{r.target ? ` · target ${r.target}` : ""}</li>
          ))}
        </ol>
      ) : null}
      <BoardTable caption={`Tesseract: ${plural(p.rounds.length, "round")} played`} head={["when", ...AXES, "pushes"]}>
        {mixes.map((m) => (
          <tr key={m.label}>
            <th scope="row">{m.label}</th>
            {AXES.map((a, k) => <td key={a}>{pct(m.mix[k] ?? 0)} %</td>)}
            <td>{p.rounds.find((x) => `round ${x.epoch}` === m.label)?.pushes.map((x) => `${x.name} ${x.text}`).join("; ") ?? ""}</td>
          </tr>
        ))}
      </BoardTable>
    </div>
  );
}
