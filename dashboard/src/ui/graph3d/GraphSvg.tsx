import { memo, useMemo, type KeyboardEvent, type Ref, type SVGAttributes } from "react";
import type { GraphEdge, GraphNode } from "./types";
import { depthOrder } from "./project";
import { placeLabels } from "./labels";
import { maxAbs, nodeName, widthOf } from "./edgeStyle";
import s from "./Graph3D.module.css";

export interface Placed {
  x: number;
  y: number;
  /** Perspective scale (1 in the flat view). */
  scale: number;
  /** Larger is nearer; the paint order. */
  depth: number;
}

/** Decor after projection: screen positions, ready to draw. */
export interface PlacedDecor {
  faces: readonly (readonly { x: number; y: number }[])[];
  lines: readonly { a: Placed; b: Placed; width: number; dashed: boolean }[];
  points: readonly { at: Placed; kind: "dot" | "ring" | "corner"; label?: string }[];
}

export interface GraphSvgProps {
  nodes: readonly GraphNode[];
  edges: readonly GraphEdge[];
  /** Aligned with `nodes`. */
  placed: readonly Placed[];
  width: number;
  height: number;
  selected?: string | null;
  onSelect?: (id: string | null) => void;
  animate: boolean;
  svgRef?: Ref<SVGSVGElement>;
  className: string;
  label: string;
  /** Pointer handlers and data attributes from the owner. */
  svgProps?: SVGAttributes<SVGSVGElement> & Record<`data-${string}`, string | number | undefined>;
  /** Labels for every node (small graphs) or only the selected one. */
  allLabels?: boolean;
  /** Layer captions for the flat view. */
  layerTags?: readonly { x: number; text: string }[];
  decor?: PlacedDecor;
}

/** Draws placed nodes and edges. Shared by the orbit view and the flat view, so they cannot disagree. */
function GraphSvgInner(p: GraphSvgProps) {
  const { nodes, edges, placed, selected, animate, svgRef } = p;
  const at = new Map<string, Placed>();
  nodes.forEach((n, i) => at.set(n.id, placed[i]));
  // Names walk every edge per node: compute them when the data changes, not at every turn.
  const names = useMemo(() => {
    const labels = new Map(nodes.map((n) => [n.id, n.label]));
    return nodes.map((n) => nodeName(n, edges, labels));
  }, [nodes, edges]);
  const wMax = maxAbs(edges.map((e) => e.weight));
  const vMax = maxAbs(nodes.map((n) => n.value)) || 1;

  const drawn = edges
    .map((e, i) => ({ e, i, a: at.get(e.from), b: at.get(e.to) }))
    .filter((x): x is { e: GraphEdge; i: number; a: Placed; b: Placed } => !!x.a && !!x.b);
  const edgeOrder = depthOrder(drawn.map((d) => (d.a.depth + d.b.depth) / 2));
  const nodeOrder = depthOrder(placed.map((q) => q.depth));

  // Every label the picture shows, placed so none overlaps another: the mix and target first, then the corners, then the nodes.
  const decorWants = (p.decor?.points ?? []).map((d) => ({ x: d.at.x, y: d.at.y, text: d.label ?? "" })).filter((d) => d.text);
  const nodeWants = nodeOrder.slice().reverse().map((k) => ({ k, x: placed[k].x, y: placed[k].y, text: nodes[k].label })).filter((n) => p.allLabels || selected === nodes[n.k].id);
  const spots = placeLabels([...decorWants, ...nodeWants], p.width, p.height);
  const decorSpot = new Map(decorWants.map((d, i) => [d.text, spots[i]] as const));
  const nodeSpot = new Map(nodeWants.map((n, i) => [n.k, spots[decorWants.length + i]] as const));

  const pick = (id: string) => p.onSelect?.(selected === id ? null : id);
  const onNodeKey = (ev: KeyboardEvent, id: string) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      ev.stopPropagation();
      pick(id);
    }
  };

  return (
    <svg
      ref={svgRef}
      className={p.className}
      viewBox={`0 0 ${p.width} ${p.height}`}
      role="group"
      aria-label={p.label}
      {...p.svgProps}
    >
      {p.decor ? (
        <g aria-hidden="true" data-decor="">
          {p.decor.faces.map((f, i) => (
            <polygon key={i} className={s.decorFace} points={f.map((q) => `${q.x},${q.y}`).join(" ")} />
          ))}
          {p.decor.lines.map((l, i) => (
            <line key={i} className={l.dashed ? `${s.decorLine} ${s.decorDashed}` : s.decorLine} x1={l.a.x} y1={l.a.y} x2={l.b.x} y2={l.b.y} strokeWidth={l.width} />
          ))}
        </g>
      ) : null}
      <g>
        {edgeOrder.map((k) => {
          const { e, i, a, b } = drawn[k];
          const sc = (a.scale + b.scale) / 2;
          const w = widthOf(e.weight, wMax) * Math.min(1.8, Math.max(0.5, sc));
          const touches = selected != null && (e.from === selected || e.to === selected);
          const dim = selected != null && !touches;
          const cls = [s.edge, e.weight < 0 ? s.neg : "", e.blocked ? s.blocked : "", dim ? s.dim : ""].filter(Boolean).join(" ");
          const flow = e.blocked ? 0 : (e.flow ?? 0);
          return (
            <g key={`${e.from}>${e.to}#${i}`} aria-hidden="true">
              <line
                className={cls}
                data-edge=""
                data-sign={e.weight < 0 ? "neg" : "pos"}
                data-blocked={e.blocked ? "1" : undefined}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                strokeWidth={w}
              />
              {flow > 0 && !dim ? (
                animate ? (
                  <line className={s.pulse} data-pulse="" x1={a.x} y1={a.y} x2={b.x} y2={b.y} strokeWidth={Math.max(1.2, w * 0.8)} />
                ) : (
                  <line className={s.flowStill} data-flow-still="" x1={a.x} y1={a.y} x2={b.x} y2={b.y} strokeWidth={w + 1 + flow * 5} />
                )
              ) : null}
            </g>
          );
        })}
      </g>
      <g aria-hidden="true">
        {nodeOrder.map((k) => {
          const n = nodes[k];
          const q = placed[k];
          const r = (3 + 7 * (Math.abs(n.value) / vMax)) * Math.min(1.8, Math.max(0.5, q.scale));
          const mix = `${Math.round(15 + 85 * (Math.abs(n.value) / vMax))}%`;
          return (
            <circle
              key={n.id}
              className={n.value < 0 ? `${s.dot} ${s.dotNeg}` : s.dot}
              style={{ "--mix": mix } as React.CSSProperties}
              data-node=""
              cx={q.x}
              cy={q.y}
              r={r}
            />
          );
        })}
      </g>
      {p.decor ? (
        <g aria-hidden="true" data-decor-points="">
          {p.decor.points.map((d, i) => (
            <g key={i}>
              {d.kind === "ring" ? (
                <circle className={s.decorRing} cx={d.at.x} cy={d.at.y} r={7 * Math.min(1.6, Math.max(0.6, d.at.scale))} />
              ) : d.kind === "corner" ? (
                <rect className={s.decorCorner} x={d.at.x - 3} y={d.at.y - 3} width={6} height={6} />
              ) : (
                <circle className={s.decorDot} cx={d.at.x} cy={d.at.y} r={4.5 * Math.min(1.6, Math.max(0.6, d.at.scale))} />
              )}
              {d.label && decorSpot.get(d.label) ? <text className={s.label} x={decorSpot.get(d.label)!.x} y={decorSpot.get(d.label)!.y} textAnchor={decorSpot.get(d.label)!.end ? "end" : "start"}>{d.label}</text> : null}
            </g>
          ))}
        </g>
      ) : null}
      {p.layerTags?.map((t) => (
        <text key={t.text} className={s.layerTag} x={t.x} y={p.height - 8} aria-hidden="true">
          {t.text}
        </text>
      ))}
      {/* The focusable layer keeps the data order, so Tab walks layer by layer whatever the angle. */}
      <g>
        {nodes.map((n, i) => {
          const q = placed[i];
          const r = (3 + 7 * (Math.abs(n.value) / vMax)) * Math.min(1.8, Math.max(0.5, q.scale)) + 2;
          const on = selected === n.id;
          return (
            <circle
              key={n.id}
              className={on ? `${s.hit} ${s.picked}` : s.hit}
              data-hit=""
              role="button"
              tabIndex={0}
              aria-pressed={on}
              aria-label={names[i]}
              cx={q.x}
              cy={q.y}
              r={r}
              onClick={(ev) => {
                ev.stopPropagation();
                pick(n.id);
              }}
              onKeyDown={(ev) => onNodeKey(ev, n.id)}
            />
          );
        })}
      </g>
      <g aria-hidden="true">
        {nodeWants.map((n) => {
          const at = nodeSpot.get(n.k);
          return at ? (
            <text key={nodes[n.k].id} className={s.label} x={at.x} y={at.y} textAnchor={at.end ? "end" : "start"}>
              {nodes[n.k].label}
            </text>
          ) : null;
        })}
      </g>
    </svg>
  );
}

export const GraphSvg = memo(GraphSvgInner);
