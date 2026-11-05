import "./badges.css";

import { useMemo } from "react";

import { CAPTURE } from "../capture";
import { resolveBadge } from "./catalog.ts";
import { ACCENT_FILL, INK_FILL, toRuns, toScale } from "./format.ts";

export interface BadgeProps {
  /** Whatever the relay or the API calls it: a quest id, a title, a class, `renown-3`… */
  id: string | null | undefined;
  /** Whole-number scale: 1× (16 px, or 12 px for a small mark), 2× or 3×. Default 2. */
  size?: 1 | 2 | 3;
  /** The tooltip's second part, in place of the catalogue's "how": e.g. "earned 3 Nov". */
  title?: string;
  /** The earn animation (a short pixel pop). Off under reduced motion and in ?capture=1. */
  pop?: boolean;
  /** Ink only (the accent drawn in currentColor), for a badge placed on a fill. */
  mono?: boolean;
  className?: string;
}

/**
 * A pixel badge: an inline SVG of `<rect>`s in two colour roles, no background.
 * `role="img"` with the badge's human name as its label; the tooltip says the name
 * and, in one line, how it was earned. Read-only: it shows, it never acts.
 */
export function Badge({ id, size = 2, title, pop = false, mono = false, className }: BadgeProps) {
  const { def, fallback } = useMemo(() => resolveBadge(id), [id]);
  const runs = useMemo(() => toRuns(def.grid), [def]);
  const scale = toScale(size);
  const tip = `${def.name} — ${(title ?? def.how).replace(/\s+/g, " ").trim()}`;
  const cls = ["e-badge", `e-badge--${def.kind}`, pop && !CAPTURE ? "e-badge--pop" : "", mono ? "e-badge--mono" : "",
    className ?? ""].filter(Boolean).join(" ");
  const ink = runs.filter((r) => r.role === "ink");
  const accent = runs.filter((r) => r.role === "accent");
  return (
    <span className={cls} role="img" aria-label={def.name} title={tip} data-badge={def.id}
      data-fallback={fallback ? "" : undefined}>
      <svg xmlns="http://www.w3.org/2000/svg" width={def.grid.w * scale} height={def.grid.h * scale}
        viewBox={`0 0 ${def.grid.w} ${def.grid.h}`} shapeRendering="crispEdges" aria-hidden="true" focusable="false">
        <g fill={INK_FILL}>
          {ink.map((r) => <rect key={`${r.x}.${r.y}`} x={r.x} y={r.y} width={r.w} height={1} />)}
        </g>
        {accent.length > 0 && (
          <g className="e-badge__accent" style={{ fill: ACCENT_FILL }}>
            {accent.map((r) => <rect key={`${r.x}.${r.y}`} x={r.x} y={r.y} width={r.w} height={1} />)}
          </g>
        )}
      </svg>
    </span>
  );
}
