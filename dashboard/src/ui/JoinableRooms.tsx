import type { Room } from "../api";
import { joinableRooms } from "../roomOffers";
import { RowTable, type RowCells } from "./RowTable";

/**
 * "Rooms you can join": the rooms on offer (free or paid), from the list the Rooms view already
 * holds. Folded away until opened, so the chat keeps the page. Picking one opens it there.
 */
export function JoinableRooms({ rooms, onOpen }: { rooms: Room[]; onOpen: (room: Room) => void }) {
  const offered = joinableRooms(rooms);
  if (offered.length === 0) return null;
  const rows: RowCells[] = offered.map((r) => [
    r.name.slice(0, 24),
    r.room_type ?? "public",
    r.gate_type ?? "open",
    r.entry_fee_cents ? `$${(r.entry_fee_cents / 100).toFixed(2)}` : "—",
    String(r.members?.length ?? r.member_count ?? 0),
  ]);
  return (
    <details className="e-joinable">
      <summary>Rooms you can join · {offered.length}</summary>
      <RowTable
        headers={["NAME", "TYPE", "GATE", "FEE", "MBR"]}
        rows={rows}
        keys={offered.map((r) => r.room_id)}
        onOpen={(i) => onOpen(offered[i])}
      />
    </details>
  );
}
