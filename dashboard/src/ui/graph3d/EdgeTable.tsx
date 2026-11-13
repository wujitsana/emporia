import type { GraphEdge, GraphNode } from "./types";
import { fmt } from "./edgeStyle";
import s from "./EdgeTable.module.css";

/** Every edge as a row: the complete, accessible form of the graph. */
export function EdgeTable({
  nodes,
  edges,
  selected,
}: {
  nodes: readonly GraphNode[];
  edges: readonly GraphEdge[];
  selected?: string | null;
}) {
  const label = new Map(nodes.map((n) => [n.id, n.label]));
  const name = (id: string) => label.get(id) ?? id;
  return (
    <div className={s.wrap}>
      <table className={s.table}>
        <caption>
          {nodes.length} nodes, {edges.length} edges
        </caption>
        <thead>
          <tr>
            <th scope="col">From</th>
            <th scope="col">To</th>
            <th scope="col" className={s.num}>
              Weight
            </th>
            <th scope="col">Sign</th>
            <th scope="col" className={s.num}>
              Width
            </th>
            <th scope="col">Blocked</th>
            <th scope="col" className={s.num}>
              Flow
            </th>
            <th scope="col">Pushed by</th>
          </tr>
        </thead>
        <tbody>
          {edges.map((e, i) => {
            const picked = selected != null && (e.from === selected || e.to === selected);
            return (
              <tr
                key={`${e.from}>${e.to}#${i}`}
                className={s.row}
                data-edge
                data-picked={picked}
              >
                <td>{name(e.from)}</td>
                <td>{name(e.to)}</td>
                <td className={s.num}>{fmt(e.weight)}</td>
                <td>{e.weight < 0 ? "negative" : e.weight > 0 ? "positive" : "zero"}</td>
                <td className={s.num}>{fmt(Math.abs(e.weight))}</td>
                <td>{e.blocked ? "yes" : "no"}</td>
                <td className={s.num}>{fmt(e.flow ?? 0)}</td>
                <td>{e.owners?.length ? e.owners.join(", ") : "none"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
