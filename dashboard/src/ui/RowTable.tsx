import type { ReactElement } from "react";

export type RowCells = (string | ReactElement)[];

/**
 * A compact table whose rows may be opened. `onOpen` is optional: a table with no
 * view to open for its rows (nothing to click through to) is drawn plain.
 */
export function RowTable({
  headers,
  rows,
  keys,
  onOpen,
}: {
  headers: string[];
  rows: RowCells[];
  keys: string[];
  onOpen?: (index: number) => void;
}) {
  return (
    <div
      className={`e-compact-table${onOpen ? " e-table-click" : ""}`}
      style={{ overflowX: "auto" }}
    >
      <table className="e-table">
        <thead>
          <tr>
            {headers.map((h) => (
              <td key={h}>{h}</td>
            ))}
          </tr>
        </thead>
        <tbody>
          {keys.map((k, i) => (
            <tr
              key={k}
              className={onOpen ? "e-table-row" : undefined}
              role={onOpen ? "button" : undefined}
              tabIndex={onOpen ? 0 : undefined}
              onClick={onOpen ? () => onOpen(i) : undefined}
              onKeyDown={
                onOpen
                  ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onOpen(i);
                      }
                    }
                  : undefined
              }
            >
              {rows[i].map((cell, j) => (
                <td key={j}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
