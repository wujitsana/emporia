import type { CSSProperties } from "react";
import { BoardTable } from "./BoardTable";
import { plural } from "./boardKit";
import kit from "./BoardKit.module.css";
import styles from "./AuctionLine.module.css";

export interface AuctionPoint { id: string; name: string; value: number }

export interface AuctionLineProps {
  min?: number;
  max?: number;
  /** Bids revealed so far (public once revealed). A sealed bid is only a count. */
  bids: AuctionPoint[];
  sealed?: number;
  /** The public base value, once the round is read. */
  base: number | null;
  /** The game is finished and the record has revealed signals and V. Without it none of them is drawn. */
  over: boolean;
  signals: AuctionPoint[] | null;
  /** V, the pot's worth. */
  value: number | null;
  /** The relay's result: who won the pot, at what bid, and the gain (negative is the winner's curse). */
  winner: { id: string; name: string; bid: number; profit: number | null } | null;
  /** The relay named a winner's curse (the bidder lost, the rest share the win). */
  curse?: boolean;
  spread?: number;
}

/** Spread labels that would overlap (about a sixth of the line) onto their own rows. */
function stack<T extends { at: number }>(items: T[], range: number): { item: T; row: number }[] {
  const last: number[] = [];
  return [...items].sort((a, b) => a.at - b.at).map((item) => {
    let row = 0;
    while (last[row] !== undefined && ((item.at - last[row]) / range) * 100 < 17) row++;
    last[row] = item.at;
    return { item, row };
  });
}

/**
 * Common-value auction: a number line 0-300. Sealed bids are a count, not positions. After the reveal:
 * bids as pins, each signal as a tick, the base value and V. The winner's gain or loss is the segment
 * between its bid and V, labelled, so the winner's curse is visible.
 */
export function AuctionLine({ min = 0, max = 300, bids, sealed = 0, base, over, signals, value, winner, spread }: AuctionLineProps) {
  const range = max - min;
  const at = (x: number) => `${((Math.min(max, Math.max(min, x)) - min) / range) * 100}%`;
  const sigs = over && signals ? signals : [];
  const v = over ? value : null;
  const pins = stack(bids.map((b) => ({ ...b, at: b.value })), range);
  const ticks = stack(sigs.map((s) => ({ ...s, at: s.value })), range);
  const gain = over && winner && v !== null ? (winner.profit ?? v - winner.bid) : null;
  const segFrom = winner && v !== null ? Math.min(winner.bid, v) : 0;
  const segTo = winner && v !== null ? Math.max(winner.bid, v) : 0;
  const gainText = gain === null ? "" : gain < 0 ? `${winner!.name} paid ${-gain} more than the pot was worth: the winner's curse`
    : gain === 0 ? `${winner!.name} broke even` : `${winner!.name} gained ${gain}`;
  const label = `Bids on a line from ${min} to ${max}: ${bids.map((b) => `${b.name} ${b.value}`).join(", ") || "none revealed"}`
    + `${sealed ? `; ${plural(sealed, "bid")} sealed` : ""}${base !== null ? `; base value ${base}` : ""}${v !== null ? `; pot worth ${v}` : ""}`;
  return (
    <div className={`${kit.board} ${styles.root}`}>
      <p className={kit.rules}>
        one pot worth the same to all, known to nobody · a private noisy signal each{spread ? ` (±${spread})` : ""}
        {" "}· one sealed bid each, the highest wins value minus bid
        {sealed ? ` · ${plural(sealed, "bid")} still sealed` : ""}{over ? "" : " · signals and the pot's worth appear when the game ends"}
      </p>
      <div className={kit.scroll}>
        <div className={styles.line} role="img" aria-label={label}>
          <div className={styles.lane} style={{ "--rows": Math.max(1, ...ticks.map((t) => t.row + 1)) } as CSSProperties}>
            {ticks.map(({ item, row }) => (
              <span key={item.id} className={styles.sig} style={{ left: at(item.at), "--row": row } as CSSProperties}>
                <span className={styles.sigText}>{item.name} signal {item.value}</span><span className={styles.sigTick} />
              </span>
            ))}
          </div>
          <div className={styles.axis}>
            {[0, 0.25, 0.5, 0.75, 1].map((f) => (
              <span key={f} className={styles.tick} style={{ left: at(min + f * range) }}><span>{Math.round(min + f * range)}</span></span>
            ))}
            {base !== null ? <span className={`${styles.mark} ${styles.base}`} style={{ left: at(base) }}><span>base {base}</span></span> : null}
            {v !== null ? <span className={`${styles.mark} ${styles.pot}`} style={{ left: at(v) }}><span>V {v}</span></span> : null}
          </div>
          <div className={styles.lane} style={{ "--rows": Math.max(1, ...pins.map((p) => p.row + 1)) } as CSSProperties}>
            {pins.map(({ item, row }) => (
              <span key={item.id} className={`${styles.pin}${winner?.id === item.id ? ` ${styles.won}` : ""}`}
                style={{ left: at(item.at), "--row": row } as CSSProperties}>
                <span className={styles.pinTick} />
                <span className={styles.pinText}>{winner?.id === item.id ? "★ " : ""}{item.name} bid {item.value}</span>
              </span>
            ))}
          </div>
          {winner && v !== null && gain !== null ? (
            <div className={styles.seg}>
              <span className={`${styles.span}${gain < 0 ? ` ${styles.loss}` : ""}`}
                style={{ left: at(segFrom), width: `${((Math.min(max, segTo) - Math.max(min, segFrom)) / range) * 100}%` }} />
              <span className={styles.segText} style={{ left: at(segFrom) }}>{gain < 0 ? `${gain} (curse)` : `+${gain}`}</span>
            </div>
          ) : null}
        </div>
      </div>
      {gainText ? <p className={kit.foot}>{gainText} (bid {winner!.bid}, pot {v})</p> : null}
      <BoardTable caption="Common-value auction" head={["player", "bid", "signal"]}>
        {bids.map((b) => (
          <tr key={b.id}>
            <th scope="row">{b.name}{winner?.id === b.id ? " (won the pot)" : ""}</th><td>{b.value}</td>
            <td>{over ? (sigs.find((s) => s.id === b.id)?.value ?? "never revealed") : "private"}</td>
          </tr>
        ))}
        {sealed ? <tr><th scope="row">sealed</th><td>{plural(sealed, "bid")} not revealed</td><td /></tr> : null}
        {!bids.length && !sealed ? <tr><th scope="row">none</th><td>no bid yet</td><td /></tr> : null}
        <tr><th scope="row">base value</th><td>{base !== null ? base : "not known yet"}</td><td /></tr>
        <tr><th scope="row">pot worth V</th><td>{v !== null ? v : "not known yet"}</td><td /></tr>
        {gain !== null ? <tr><th scope="row">winner&apos;s result</th><td>{gain}</td><td>{gainText}</td></tr> : null}
      </BoardTable>
    </div>
  );
}
