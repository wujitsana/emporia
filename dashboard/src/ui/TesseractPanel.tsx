import { useEffect, useMemo, useState } from "react";
import { motionAllowed } from "../look";
import { HUBS, INPUTS, OUTPUTS, type TesseractFrame } from "../tesseract";
import {
  ALL, DRAIN, PULSE, boardStates, corner4, cornerPositions, distance, mix4, ownersPublic, phaseNote, place3, target4, targetsKnown,
  type BoardState,
} from "../tesseractView";
import { Graph3D } from "./graph3d/Graph3D";
import type { GraphDecor, GraphEdge, GraphNode, GraphViewMode } from "./graph3d/types";
import s from "./TesseractPanel.module.css";

const pct = (bp: number) => (bp / 100).toFixed(1);
const signed = (n: number) => (n > 0 ? `+${n}` : n < 0 ? `−${Math.abs(n)}` : "0");
const pullText = (v: number[]) => v.map((x, k) => `${OUTPUTS[k]} ${x > 0 ? "+" : x < 0 ? "−" : ""}${pct(Math.abs(x))}`).join(", ");

/** The four shares as bars, each with a tick where the chosen target sits (only once targets are public). */
function MixBars({ mix, target }: { mix: number[]; target: number[] | null }) {
  return (
    <div className={s.bars} role="group" aria-label="The mix: each output's share of the flow">
      {OUTPUTS.map((o, k) => (
        <div key={o} className={s.barRow}>
          <span className={s.barLabel}>{o}</span>
          <span
            className={s.bar}
            role="img"
            aria-label={`${o}: ${pct(mix[k] ?? 0)} percent${target ? `, target ${target[k]} percent` : ""}`}
          >
            <span className={s.fill} style={{ width: `${Math.min(100, (mix[k] ?? 0) / 50)}%` }} />
            {target ? <span className={s.tick} data-tick="" style={{ left: `${Math.min(100, (target[k] * 100) / 50)}%` }} /> : null}
          </span>
          <span className={s.barValue}>{pct(mix[k] ?? 0)}%</span>
        </div>
      ))}
    </div>
  );
}

/** The 24 channels as a real table: the complete accessible form of the network. */
function ChannelTable({ st, name, live }: { st: BoardState; name: (id: string) => string; live: boolean }) {
  return (
    <div className={s.tableWrap}>
      <table className={s.table}>
        <caption>
          The 24 channels, {st.step === 0 ? "as opened" : `after round ${st.step}`}{st.darkCount ? `; ${st.darkCount} carried nothing that round` : ""}.
          {" "}Share change: how each output's share would move, in points, if one more push of +4 (or −4) went on that channel.
        </caption>
        <thead>
          <tr>
            <th scope="col">Channel</th>
            <th scope="col" className={s.num}>Width</th>
            <th scope="col" className={s.num}>Net push</th>
            <th scope="col">Pushed by</th>
            <th scope="col">Carried nothing</th>
            <th scope="col">Share change for +4</th>
            <th scope="col">Share change for −4</th>
          </tr>
        </thead>
        <tbody>
          {st.channels.map((c) => (
            <tr key={c.id} data-channel={c.id} data-dark={c.dark}>
              <th scope="row">{c.from} to {c.to}</th>
              <td className={s.num}>{c.width}</td>
              <td className={s.num}>{st.step === 0 ? "n/a" : signed(c.net)}</td>
              <td>{c.pushes.length ? c.pushes.map((x) => `${name(x.agent)} ${signed(x.amount)}`).join(", ") : st.step === 0 ? "n/a" : "no one"}</td>
              <td>{st.step === 0 ? "n/a" : c.dark ? "yes" : "no"}</td>
              <td>{live && c.pull ? pullText(c.pull.up) : "n/a"}</td>
              <td>{live && c.pull ? pullText(c.pull.down) : "n/a"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The Tesseract table as a picture: a flat view (inputs, hubs, outputs and the mix as four bars), the table of the 24
 * channels, and the board as the 3D tesseract on the graph kit. Read-only: it draws the frame the Arena gives it, which
 * holds only what the record shows at that step, so a sealed round shows no pushes and no target appears before the end.
 */
export function TesseractPanel({ frame, name }: { frame: TesseractFrame; name: (id: string) => string }) {
  const [pick, setPick] = useState<number | null>(null);
  const [who, setWho] = useState<string>(ALL);
  const [tgt, setTgt] = useState<string | null>(null);
  const [mode, setMode] = useState<GraphViewMode>("flat");
  const [paused, setPaused] = useState(false);
  const [turn, setTurn] = useState(0);

  const owners = ownersPublic(frame);
  const states = useMemo(() => boardStates(frame, owners ? who : ALL), [frame, who, owners]);
  const last = states.length - 1;
  const at = pick === null || pick > last ? last : pick;
  const st = states[at];
  const targets = targetsKnown(frame);
  const targetIds = Object.keys(targets);
  const chosen = tgt && targets[tgt] ? tgt : (frame.result?.winner && targets[frame.result.winner] ? frame.result.winner : (targetIds[0] ?? null));
  const target = chosen ? targets[chosen] : null;
  const live = at === last;

  // About one turn a minute, only while the 3D view shows, never under reduced motion, stopped by any input.
  const spin = mode === "3d" && !paused && motionAllowed();
  useEffect(() => {
    if (!spin) return;
    const start = Date.now();
    const id = setInterval(() => setTurn(((Date.now() - start) / 60000) * Math.PI * 2), 80);
    return () => clearInterval(id);
  }, [spin]);

  const a = turn, b = turn * 0.6;
  const nodes: GraphNode[] = useMemo(() => {
    const mix = st?.mix ?? [];
    return [
      ...INPUTS.map((id, index) => ({ id, layer: 0, index, label: id, value: 0 })),
      ...HUBS.map((id, index) => ({ id, layer: 1, index, label: id, value: 0 })),
      ...OUTPUTS.map((id, index) => ({ id, layer: 2, index, label: `${id} ${pct(mix[index] ?? 0)}%`, value: (mix[index] ?? 0) / 100 })),
    ];
  }, [st]);
  const edges: GraphEdge[] = (st?.channels ?? []).map((c) => ({
    from: c.from, to: c.to, weight: c.width, blocked: c.dark,
    owners: c.pushes.map((x) => `${name(x.agent)} ${signed(x.amount)}`),
  }));
  const positions = useMemo(() => cornerPositions(a, b), [a, b]);
  const decor: GraphDecor = (() => {
    const p = (n: string | number[], isMix = false) => place3(isMix ? mix4(n as number[]) : corner4(n as string), a, b);
    const outs = OUTPUTS.map((o) => p(o));
    const trail = states.map((x) => place3(mix4(x.mix), a, b));
    const lines: NonNullable<GraphDecor["lines"]>[number][] = [];
    for (let i = 0; i < 4; i++) for (let j = i + 1; j < 4; j++) lines.push({ a: outs[i], b: outs[j], width: 1 });
    // the 4 intake edges (fixed) and the 4 drain edges (as thick as each output's share)
    INPUTS.forEach((c) => lines.push({ a: p(PULSE), b: p(c), width: 1, dashed: true }));
    OUTPUTS.forEach((o, k) => lines.push({ a: p(o), b: p(DRAIN), width: 0.6 + ((st?.mix[k] ?? 0) / 10000) * 8, dashed: true }));
    for (let i = 1; i < trail.length; i++) lines.push({ a: trail[i - 1], b: trail[i], width: 1.6 });
    const points: NonNullable<GraphDecor["points"]>[number][] = [
      { p: p(PULSE), kind: "corner", label: "pulse" },
      { p: p(DRAIN), kind: "corner", label: "drain" },
    ];
    if (st) points.push({ p: p(st.mix, true), kind: "dot", label: "mix" });
    if (target) points.push({ p: place3(target4(target), a, b), kind: "ring", label: chosen ? `target ${name(chosen)}` : "target" });
    const faces = [[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]].map((f) => ({ pts: f.map((i) => outs[i]) }));
    return { faces, lines, points };
  })();

  if (!st) {
    return (
      <div className={s.root} data-testid="tesseract-panel">
        {frame.practice ? <p className={s.note} role="note"><strong>Practice table: counts for nothing</strong></p> : null}
        <p className={s.note}>The network appears once all seats are in and the public random draw it uses is published. {phaseNote(frame)}</p>
      </div>
    );
  }

  const dist = target ? distance(st.mix, target) : null;
  return (
    <div
      className={s.root}
      data-testid="tesseract-panel"
      onPointerDownCapture={() => mode === "3d" && setPaused(true)}
      onKeyDownCapture={() => mode === "3d" && setPaused(true)}
    >
      {frame.practice ? <p className={s.note} role="note"><strong>Practice table: counts for nothing</strong> (no standing, XP, badges or quests)</p> : null}
      <p className={s.note}>{phaseNote(frame)}</p>
      <div className={s.controls}>
        <div className={s.seg} role="group" aria-label="Which network">
          {states.map((x, i) => (
            <button key={x.step} type="button" className={s.segBtn} aria-pressed={at === i} onClick={() => setPick(i === last ? null : i)}>
              {x.label}
            </button>
          ))}
        </div>
        {owners ? (
          <label className={s.field}>
            Pushes by{" "}
            <select value={who} onChange={(e) => setWho(e.target.value)}>
              <option value={ALL}>everyone</option>
              {frame.players.map((p) => <option key={p} value={p}>{name(p)}</option>)}
            </select>
          </label>
        ) : null}
        {targetIds.length ? (
          <label className={s.field}>
            Target of{" "}
            <select value={chosen ?? ""} onChange={(e) => setTgt(e.target.value)}>
              {targetIds.map((p) => <option key={p} value={p}>{name(p)}</option>)}
            </select>
          </label>
        ) : null}
      </div>
      <MixBars mix={st.mix} target={target} />
      <p className={s.dist}>
        {st.label === "opening" ? "Opening mix" : `Mix after ${st.label}`}: {OUTPUTS.map((o, k) => `${o} ${pct(st.mix[k] ?? 0)}%`).join(" · ")}
        {dist !== null && chosen ? `. ${name(chosen)}'s target is ${OUTPUTS.map((o, k) => `${o} ${target![k]}%`).join(" · ")}: the mix is ${pct(dist)} points away (the four gaps added up).` : ""}
      </p>
      <Graph3D
        nodes={nodes}
        edges={edges}
        initialMode="flat"
        label="Tesseract network"
        layerNames={["4 inputs", "6 hubs", "4 outputs"]}
        positions={positions}
        decor={decor}
        onMode={setMode}
        animate={motionAllowed()}
        tableView={<ChannelTable st={st} name={name} live={live} />}
      />
      <p className={s.caption}>
        The 3D view is the tesseract itself: 16 corners and 32 edges. The four outputs, W, X, Y and Z, are its four axes. The mix is the dot
        inside the shaded four-corner solid they span; the line behind it is its path from round to round, and a ring is a target (shown only
        when the table is over). Use it to see which corner the mix is heading toward, not how far: distances shrink and change as it turns.
        For how far, read the number above and the bars with their ticks.
      </p>
    </div>
  );
}
