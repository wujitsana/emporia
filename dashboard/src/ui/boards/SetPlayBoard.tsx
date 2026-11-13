import { BlottoBars, type BlottoSeat } from "./BlottoBars";
import { PickLine } from "./PickLine";
import { UniqueRow } from "./UniqueRow";
import type { BoardPick } from "./boardKit";
import type { SetPlayBoardModel } from "./setPlayModel";

export function SetPlayBoard({ model: m }: { model: SetPlayBoardModel }) {
  if (m.kind === "blotto") {
    const seats: BlottoSeat[] = m.seats.map((s) => ({
      id: s.id, name: s.name, state: s.state, winner: s.winner, note: s.note,
      alloc: s.state === "revealed" && Array.isArray(s.value) ? s.value : undefined,
    }));
    return (
      <BlottoBars seats={seats} fields={m.facts.battlefields} troops={m.facts.troops} weights={m.facts.weights}
        fieldWinners={m.fieldWinners} round={m.round} rounds={m.rounds} roundWins={m.roundWins} />
    );
  }
  const picks: BoardPick[] = m.seats.flatMap((s) =>
    s.state === "revealed" && typeof s.value === "number" ? [{ id: s.id, name: s.name, value: s.value, winner: !!s.winner }] : []);
  const hidden = m.seats.filter((s) => s.state === "sealed").length;
  if (m.kind === "two-thirds") {
    return <PickLine min={m.facts.min} max={m.facts.max} picks={picks} hidden={hidden}
      average={m.facts.average} target={m.facts.target} draw={!!m.verdict?.draw} />;
  }
  return <UniqueRow min={m.facts.min} max={m.facts.max} picks={picks} hidden={hidden} draw={!!m.verdict?.draw} />;
}
