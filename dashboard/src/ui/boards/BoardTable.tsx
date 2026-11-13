import type { ReactNode } from "react";
import styles from "./BoardKit.module.css";

/**
 * A board's text alternative: a real table, visually hidden, that states every number the drawing
 * shows. Pass the header cells and the body rows.
 */
export function BoardTable({ caption, head, children }: { caption: string; head: string[]; children: ReactNode }) {
  return (
    <table className={styles.sr}>
      <caption>{caption}</caption>
      <thead>
        <tr>{head.map((h) => <th key={h} scope="col">{h}</th>)}</tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  );
}
