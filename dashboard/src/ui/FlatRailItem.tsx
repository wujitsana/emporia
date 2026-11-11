import type { ReactNode } from "react";
import { listItemSelectStyle } from "./listSelection";

type FlatRailItemProps = {
  selected?: boolean;
  onClick?: () => void;
  /** A filter, not an item: on a phone it narrows the list and stays on it
   *  instead of opening a detail screen (see kit SidebarLayout). */
  keepList?: boolean;
  /** Drawn as one of the page's cards (corner brackets, a tint fill when picked): topics and rooms. */
  card?: boolean;
  children: ReactNode;
};

/** Left-rail row — no SRCL ActionListItem gray/amber gutters. */
export function FlatRailItem({ selected, onClick, keepList, card, children }: FlatRailItemProps) {
  return (
    <button
      type="button"
      className={`e-rail-item${card ? " e-rail-item--card" : ""}${selected ? " is-selected" : ""}`}
      onClick={onClick}
      style={listItemSelectStyle(!!selected)}
      aria-current={selected ? "true" : undefined}
      data-keep-list={keepList ? "" : undefined}
    >
      {children}
    </button>
  );
}
