import type { ReactNode } from "react";

/**
 * A finished game's panel leads with who played and the result; the moves, hands and rationales sit behind this
 * one line. While the game runs (`fold` false) the detail is just shown, nothing hidden.
 */
export function FoldDetail({ fold, label, children }: { fold: boolean; label: string; children: ReactNode }) {
  if (!fold) return <>{children}</>;
  return (
    <details className="e-fold e-fold--game">
      <summary>{label}</summary>
      {children}
    </details>
  );
}
