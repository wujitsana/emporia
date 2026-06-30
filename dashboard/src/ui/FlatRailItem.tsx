import type { ReactNode } from "react";
import { listItemSelectStyle } from "./listSelection";

type FlatRailItemProps = {
  selected?: boolean;
  onClick?: () => void;
  children: ReactNode;
};

/** Left-rail row — no SRCL ActionListItem gray/amber gutters. */
export function FlatRailItem({ selected, onClick, children }: FlatRailItemProps) {
  return (
    <button
      type="button"
      className={`e-rail-item${selected ? " is-selected" : ""}`}
      onClick={onClick}
      style={listItemSelectStyle(!!selected)}
    >
      {children}
    </button>
  );
}