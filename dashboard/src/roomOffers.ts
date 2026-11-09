import type { Room } from "./api";

/** A room anyone may open: public, or paid. A private room is not on offer. */
export function joinableRooms(rooms: Room[]): Room[] {
  return rooms.filter((r) => r.room_type === "public" || r.gate_type === "paid");
}
