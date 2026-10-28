/**
 * P249: rooms and Agora topics that linked relays advertised, as rows the
 * "On other relays" section shows.
 *
 * Pure, so it can be tested without a component tree. The dashboard only reads
 * these: joining and paying happen on the relay that holds the room or topic,
 * which is why every row names it.
 */
import type { RoomElsewhere, TopicElsewhere } from "./api";
import { relayHost } from "./relayFormat";

export interface ElsewhereRow {
  key: string;
  name: string;
  /** The relay that holds it — where it is joined and paid for. */
  host: string;
  owner: string;
  fingerprint: string;
  /** "free" or "$5.00". */
  fee: string;
  note: string;
}

/** A fee in words: "free", "$5.00", or "$5.00 EUR" for a currency other than USD. */
export function feeLabel(cents: number | undefined | null, currency?: string | null): string {
  if (!cents || cents <= 0) return "free";
  const amount = `$${(cents / 100).toFixed(2)}`;
  const cur = (currency ?? "").toUpperCase();
  return cur && cur !== "USD" ? `${amount} ${cur}` : amount;
}

function row(key: string, name: string, relay: string, host: string | undefined, owner: string | undefined,
             fingerprint: string | undefined, fee: string, extra: string): ElsewhereRow {
  const where = relayHost(host || relay) || "another relay";
  return {
    key, name, host: where, owner: owner ?? "", fingerprint: fingerprint ?? "", fee,
    note: `${extra}Joining happens on ${where}, not here.`,
  };
}

export function roomRow(r: RoomElsewhere): ElsewhereRow {
  const access = r.access_duration_days ? `${r.access_duration_days} days of access per payment. ` : "";
  return row(`${r.relay}/${r.room_id}`, r.name, r.relay, r.host, r.owner_address, r.owner_fingerprint,
    feeLabel(r.entry_fee_cents, r.currency), access);
}

export function topicRow(t: TopicElsewhere): ElsewhereRow {
  return row(`${t.relay}/${t.slug}`, t.name, t.relay, t.host, t.owner_address, t.owner_fingerprint,
    t.gate_type === "paid_invite" ? feeLabel(t.entry_fee_cents) : "free", "");
}
