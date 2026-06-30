import type React from "react";

/** ActionListItem does not accept className — use borderLeft for .e-sel parity */
export function listItemSelectStyle(selected: boolean): React.CSSProperties {
  return {
    cursor: "pointer",
    borderLeft: selected
      ? "2px solid var(--theme-focused-foreground)"
      : "2px solid transparent",
    paddingLeft: 8,
  };
}