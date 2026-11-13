import { GOODS, type Goods } from "../../bargain";
import { BoardTable } from "./BoardTable";
import { plural } from "./boardKit";
import kit from "./BoardKit.module.css";
import styles from "./BargainSplit.module.css";

export interface BargainSide { id: string; name: string }
export interface BargainSplitOffer {
  n: number;
  byName: string;
  /** What each side gets, by player id. A side missing here is the rest of the pool. */
  give: Record<string, Goods>;
  /** What the offerer said its own values were: unchecked until the end. */
  claim?: Goods;
}
export interface BargainClaim { claimed: Goods; actual: Goods; matches: boolean }

export interface BargainSplitProps {
  /** The two players, in the order the split is drawn: first solid, second hatched. */
  sides: BargainSide[];
  pool: Goods;
  offers: BargainSplitOffer[];
  /** The offer number that was accepted, if any. */
  accepted: number | null;
  /** The game is finished: only then are hands, points and claims drawn. Without it none of them is. */
  over: boolean;
  /** The real hands, published with the record. Dropped unless `over`. */
  hands: Record<string, Goods> | null;
  /** Each side's points and walk-away from the relay's result. Dropped unless `over`. */
  points: Record<string, number> | null;
  walkAways: Record<string, number> | null;
  claims: Record<string, BargainClaim> | null;
  /** How it ended, for the footer. */
  how: "accepted" | "declined" | null;
}

const units = (g: Goods | undefined, k: (typeof GOODS)[number]) => g?.[k] ?? 0;
const goodsText = (g: Goods | undefined) => GOODS.map((k) => `${units(g, k)} ${k}`).join(", ");

/**
 * Bargain: per offer, per good, a row of unit cells split between the two players (solid cells and
 * hatched cells, with the counts printed). The accepted offer is marked. After the end: each side's
 * points as a bar against its walk-away, and each hand's values beside what it claimed. Private hands
 * are never drawn before `over`.
 */
export function BargainSplit({ sides, pool, offers, accepted, over, hands, points, walkAways, claims, how }: BargainSplitProps) {
  const [a, b] = sides;
  const shownHands = over ? hands : null;
  const shownPoints = over ? points : null;
  const scale = Math.max(1, ...Object.values(shownPoints ?? {}), ...Object.values(over ? walkAways ?? {} : {}));
  const share = (o: BargainSplitOffer, side: BargainSide | undefined, k: (typeof GOODS)[number]) => {
    if (!side) return 0;
    const own = o.give[side.id];
    if (own) return units(own, k);
    const other = sides.find((s) => s.id !== side.id);
    return Math.max(0, units(pool, k) - units(other ? o.give[other.id] : undefined, k));
  };
  return (
    <div className={`${kit.board} ${styles.root}`}>
      <p className={kit.rules}>
        pool {goodsText(pool)} · each offer splits it · solid cells go to {a?.name ?? "the first player"}, hatched cells to {b?.name ?? "the second"}
        {over ? "" : " · each player's values stay private until the end"}
      </p>
      {offers.length ? (
        <div className={kit.scroll}>
          <div className={styles.offers} aria-hidden>
            {offers.map((o) => (
              <div key={o.n} className={`${styles.offer}${accepted === o.n ? ` ${styles.taken}` : ""}`}>
                <p className={styles.head}>#{o.n} {o.byName}{accepted === o.n ? " ✓ accepted" : ""}</p>
                {GOODS.map((k) => {
                  const total = units(pool, k);
                  const mine = share(o, a, k);
                  const theirs = share(o, b, k);
                  return (
                    <div key={k} className={styles.good}>
                      <span className={styles.name}>{k}</span>
                      <span className={styles.cells}>
                        {Array.from({ length: Math.max(total, mine + theirs) }, (_, i) => (
                          <span key={i} className={`${styles.unit} ${i < mine ? styles.a : i < mine + theirs ? styles.b : styles.free}`} />
                        ))}
                      </span>
                      <span className={styles.count}>{mine} | {theirs}</span>
                    </div>
                  );
                })}
                {o.claim ? <p className={styles.claim}>said its values were {goodsText(o.claim)} (unchecked)</p> : null}
              </div>
            ))}
          </div>
        </div>
      ) : <p className={kit.foot}>No offer yet.</p>}
      {over && how ? <p className={kit.foot}>{how === "accepted" ? `deal: offer ${accepted ?? offers.length} accepted` : "no deal: the last offer was declined"}</p> : null}

      {shownPoints ? (
        <div className={styles.scores} aria-hidden>
          {sides.map((s) => {
            const p = shownPoints[s.id];
            const w = over ? walkAways?.[s.id] : undefined;
            return (
              <div key={s.id} className={styles.score}>
                <span className={styles.who}>{s.name}</span>
                <span className={styles.track}>
                  <span className={styles.fill} style={{ width: `${((p ?? 0) / scale) * 100}%` }} />
                  {typeof w === "number" ? <span className={styles.walk} style={{ left: `${(w / scale) * 100}%` }} /> : null}
                </span>
                <span className={styles.pts}>{p ?? 0} points{typeof w === "number" ? ` · walk-away ${w}` : ""}</span>
              </div>
            );
          })}
        </div>
      ) : null}
      {shownHands && Object.keys(shownHands).length ? (
        <ul className={styles.hands} aria-hidden>
          {sides.map((s) => {
            const c = claims?.[s.id];
            return shownHands[s.id] ? (
              <li key={s.id}>{s.name} held {goodsText(shownHands[s.id])}{c ? ` · said ${goodsText(c.claimed)} — ${c.matches ? "matched" : "differed"}` : ""}</li>
            ) : null;
          })}
        </ul>
      ) : null}

      <BoardTable caption={`Bargain: ${plural(offers.length, "offer")}`}
        head={["offer", "by", `${a?.name ?? "first"} gets`, `${b?.name ?? "second"} gets`, "stated values", "status"]}>
        {offers.map((o) => (
          <tr key={o.n}>
            <th scope="row">{o.n}</th><td>{o.byName}</td>
            <td>{GOODS.map((k) => `${share(o, a, k)} ${k}`).join(", ")}</td>
            <td>{GOODS.map((k) => `${share(o, b, k)} ${k}`).join(", ")}</td>
            <td>{o.claim ? `${goodsText(o.claim)} (stated, unchecked)` : "none"}</td>
            <td>{accepted === o.n ? "accepted" : ""}</td>
          </tr>
        ))}
        {!offers.length ? <tr><th scope="row">none</th><td /><td /><td /><td>no offer yet</td><td /></tr> : null}
        {sides.map((s) => (
          <tr key={`end-${s.id}`}>
            <th scope="row">{s.name}</th><td>{over ? "end" : "values"}</td>
            <td colSpan={2}>{shownHands?.[s.id] ? `held ${goodsText(shownHands[s.id])}` : over ? "hand not published" : "private until the end"}</td>
            <td>{over && claims?.[s.id] ? `said ${goodsText(claims[s.id].claimed)}: ${claims[s.id].matches ? "matched" : "differed"}` : ""}</td>
            <td>{shownPoints ? `${shownPoints[s.id] ?? 0} points${typeof walkAways?.[s.id] === "number" ? `, walk-away ${walkAways[s.id]}` : ""}` : ""}</td>
          </tr>
        ))}
      </BoardTable>
    </div>
  );
}
