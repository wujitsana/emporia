import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { motionAllowed } from "../../look";
import type { GraphDecor, GraphEdge, GraphNode, GraphViewMode } from "./types";
import { DEFAULT_VIEW, clampView, layerLayout, orbitBy, panBy, project, projectAll, zoomBy, type Vec3, type View } from "./project";
import { GraphSvg, type Placed, type PlacedDecor } from "./GraphSvg";
import { FlatGraph } from "./FlatGraph";
import { FLAT_H, FLAT_W } from "./flatLayout";
import { EdgeTable } from "./EdgeTable";
import s from "./Graph3D.module.css";

const BOX = { width: FLAT_W, height: FLAT_H } as const;
const KEY_STEP = 0.12;
const PAN_STEP = 24;

export interface Graph3DProps {
  nodes: readonly GraphNode[];
  edges: readonly GraphEdge[];
  selected?: string | null;
  onSelect?: (id: string | null) => void;
  /** Pulse along edges that carry flow. Defaults to what the viewer and the capture mode allow. */
  animate?: boolean;
  /** Which view opens: the orbit view, the flat diagram, or the table. Flat when motion is off. */
  initialMode?: GraphViewMode;
  label?: string;
  /** Node positions in 3D, in place of the layered layout (a node missing here is put at the origin). */
  positions?: ReadonlyMap<string, Vec3>;
  /** Extra geometry drawn through the same projection (3D view only). */
  decor?: GraphDecor;
  /** Names for the flat view's columns (default "layer 1", ...). */
  layerNames?: readonly string[];
  /** A table in place of the generic edge table. */
  tableView?: ReactNode;
  /** Told when the view mode changes (and once at the start). */
  onMode?: (m: GraphViewMode) => void;
}

const MODES: { id: GraphViewMode; text: string }[] = [
  { id: "3d", text: "3D" },
  { id: "flat", text: "Flat" },
  { id: "table", text: "Table" },
];

/**
 * A dependency-free 3D graph viewer on SVG: drag to turn, shift- or right-drag (two fingers) to pan,
 * wheel or pinch to zoom, arrows / + / - / R from the keyboard. A flat layered view and a table
 * show the same edges. Read-only: it draws what it is given and reports a selection.
 */
export function Graph3D(props: Graph3DProps) {
  const { nodes, edges } = props;
  const animate = props.animate ?? motionAllowed();
  const [mode, setMode] = useState<GraphViewMode>(props.initialMode ?? (animate ? "3d" : "flat"));
  const [view, setView] = useState<View>(DEFAULT_VIEW);
  const [inner, setInner] = useState<string | null>(null);
  const selected = props.selected !== undefined ? props.selected : inner;
  const select = useCallback(
    (id: string | null) => {
      setInner(id);
      props.onSelect?.(id);
    },
    [props],
  );

  const layered = useMemo(() => layerLayout(nodes), [nodes]);
  const home = props.positions ?? layered;
  const placed: Placed[] = useMemo(() => {
    const pts = nodes.map((n) => home.get(n.id) ?? ([0, 0, 0] as const));
    return projectAll(pts, view, BOX);
  }, [nodes, home, view]);
  const placedDecor: PlacedDecor | undefined = useMemo(() => {
    const d = props.decor;
    if (!d) return undefined;
    const at = (p: Vec3) => project(p, view, BOX);
    return {
      faces: (d.faces ?? []).map((f) => f.pts.map((p) => at(p))),
      lines: (d.lines ?? []).map((l) => ({ a: at(l.a), b: at(l.b), width: l.width ?? 1, dashed: !!l.dashed })),
      points: (d.points ?? []).map((q) => ({ at: at(q.p), kind: q.kind, label: q.label })),
    };
  }, [props.decor, view]);
  const { onMode } = props;
  useEffect(() => onMode?.(mode), [mode, onMode]);

  // Pointer tracking: one pointer turns (or pans with shift / right button), two pinch and pan.
  const svgRef = useRef<SVGSVGElement | null>(null);
  const ptrs = useRef(new Map<number, { x: number; y: number }>());
  const moved = useRef(0);

  const toBox = (px: number, py: number) => {
    const r = svgRef.current?.getBoundingClientRect();
    const k = r && r.width > 0 ? BOX.width / r.width : 1;
    return { x: px * k, y: py * k };
  };

  const onPointerDown = (ev: PointerEvent<SVGSVGElement>) => {
    ptrs.current.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
    if (ptrs.current.size === 1) moved.current = 0;
  };
  const onPointerMove = (ev: PointerEvent<SVGSVGElement>) => {
    const prev = ptrs.current.get(ev.pointerId);
    if (!prev) return;
    const cur = { x: ev.clientX, y: ev.clientY };
    if (ptrs.current.size >= 2) {
      const other = [...ptrs.current.entries()].find(([id]) => id !== ev.pointerId)?.[1];
      if (other) {
        const d0 = Math.hypot(prev.x - other.x, prev.y - other.y);
        const d1 = Math.hypot(cur.x - other.x, cur.y - other.y);
        const mid = toBox((cur.x - prev.x) / 2, (cur.y - prev.y) / 2);
        moved.current += 10;
        setView((v) => panBy(d0 > 0 ? zoomBy(v, d1 / d0) : v, mid.x, mid.y));
      }
    } else {
      const d = toBox(cur.x - prev.x, cur.y - prev.y);
      moved.current += Math.abs(cur.x - prev.x) + Math.abs(cur.y - prev.y);
      if (ev.shiftKey || (ev.buttons & 2) !== 0) setView((v) => panBy(v, d.x, d.y));
      else setView((v) => orbitBy(v, d.x * 0.008, d.y * 0.008));
    }
    ptrs.current.set(ev.pointerId, cur);
    // Capture only once it is a drag, so a plain click still lands on the node under it.
    if (moved.current > 4 && !ev.currentTarget.hasPointerCapture?.(ev.pointerId)) {
      try {
        ev.currentTarget.setPointerCapture?.(ev.pointerId);
      } catch {
        /* a synthetic pointer */
      }
    }
  };
  const onPointerEnd = (ev: PointerEvent<SVGSVGElement>) => {
    ptrs.current.delete(ev.pointerId);
  };
  // A drag that ended on the background is not a click; a plain click there clears the selection.
  const onClick = () => {
    if (moved.current < 5 && selected != null) select(null);
  };

  // The wheel needs a non-passive listener to stop the page scrolling under it.
  useEffect(() => {
    const el = svgRef.current;
    if (!el || mode !== "3d") return;
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      setView((v) => zoomBy(v, Math.exp(-ev.deltaY * 0.0015)));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [mode]);

  const onKeyDown = (ev: KeyboardEvent<HTMLDivElement>) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey || mode !== "3d") return;
    const pan = ev.shiftKey;
    let next: View | null = null;
    switch (ev.key) {
      case "ArrowLeft":
        next = pan ? panBy(view, -PAN_STEP, 0) : orbitBy(view, -KEY_STEP, 0);
        break;
      case "ArrowRight":
        next = pan ? panBy(view, PAN_STEP, 0) : orbitBy(view, KEY_STEP, 0);
        break;
      case "ArrowUp":
        next = pan ? panBy(view, 0, -PAN_STEP) : orbitBy(view, 0, -KEY_STEP);
        break;
      case "ArrowDown":
        next = pan ? panBy(view, 0, PAN_STEP) : orbitBy(view, 0, KEY_STEP);
        break;
      case "+":
      case "=":
        next = zoomBy(view, 1.2);
        break;
      case "-":
      case "_":
        next = zoomBy(view, 1 / 1.2);
        break;
      case "r":
      case "R":
      case "0":
        next = DEFAULT_VIEW;
        break;
      default:
        return;
    }
    ev.preventDefault();
    setView(clampView(next));
  };

  const label = props.label ?? "Graph";
  return (
    <div className={s.root}>
      <div className={s.bar}>
        <div className={s.switch} role="group" aria-label="View">
          {MODES.map((m) => (
            <button key={m.id} type="button" className={s.switchBtn} aria-pressed={mode === m.id} onClick={() => setMode(m.id)}>
              {m.text}
            </button>
          ))}
        </div>
        {mode === "3d" ? (
          <button type="button" className={s.resetBtn} onClick={() => setView(DEFAULT_VIEW)}>
            Reset view
          </button>
        ) : null}
        <span className={s.hint}>
          {mode === "3d"
            ? "Drag to turn, shift-drag to pan, wheel to zoom. Keys: arrows, + / -, R."
            : mode === "flat"
              ? "Layers left to right."
              : "Every edge, with its weight."}
        </span>
      </div>
      {mode === "3d" ? (
        <div className={s.stage} onKeyDown={onKeyDown} tabIndex={0} role="group" aria-label={`${label}: arrow keys turn, shift and arrows pan, plus and minus zoom, R resets`} data-testid="graph3d-stage">
          <GraphSvg
            nodes={nodes}
            edges={edges}
            placed={placed}
            width={BOX.width}
            height={BOX.height}
            selected={selected}
            onSelect={select}
            animate={animate}
            svgRef={svgRef}
            className={s.svg}
            label={`${label}, 3D view`}
            allLabels={nodes.length <= 24}
            decor={placedDecor}
            svgProps={{
              onPointerDown,
              onPointerMove,
              onPointerUp: onPointerEnd,
              onPointerCancel: onPointerEnd,
              onClick,
              onContextMenu: (ev) => ev.preventDefault(),
              "data-yaw": view.yaw.toFixed(3),
              "data-pitch": view.pitch.toFixed(3),
              "data-zoom": view.zoom.toFixed(3),
              "data-pan": `${Math.round(view.panX)},${Math.round(view.panY)}`,
            }}
          />
        </div>
      ) : mode === "flat" ? (
        <div className={s.stage}>
          <FlatGraph nodes={nodes} edges={edges} selected={selected} onSelect={select} animate={animate} label={`${label}, flat view`} layerNames={props.layerNames} />
        </div>
      ) : (
        props.tableView ?? <EdgeTable nodes={nodes} edges={edges} selected={selected} />
      )}
    </div>
  );
}
