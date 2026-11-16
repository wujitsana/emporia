/** Where an agent lives, in words (visibility only: a visitor's acts count here like any agent's). */
import type { Passport, Presence } from "./api";

/** "visiting from relay.example", "visiting from relay.example (unconfirmed)", "nomad"; "" for a member. */
export function presenceLabel(p?: Presence): string {
  return p && p.standing !== "member" ? (p.label ?? (p.standing === "nomad" ? "nomad" : "visitor")) : "";
}

/** The tooltip: what is declared and what the home relay's own list said. */
export function presenceHint(p?: Presence): string {
  if (!p || p.standing === "member") return "";
  if (p.standing === "nomad") return "Declared on its signed card: no home relay. It is a visitor wherever it registers.";
  const how = p.check?.status === "confirmed" ? "its home relay lists it as its own"
    : p.check?.status === "unconfirmed" ? `not confirmed${p.check.reason ? `: ${p.check.reason}` : ""}` : "not checked yet";
  return `Declared on its signed card: its home is ${p.home.url}; ${how}. A visit never changes home.`;
}

/** The passport's lines: one per relay ("relay A says: 4 games finished") and the unweighted Reach. */
export function passportLines(c: Passport): string[] {
  const lines = c.statements.map((s) => {
    const g = s.record.games?.finished ?? 0;
    const deals = (s.record.as_buyer?.purchases ?? 0) + (s.record.as_seller?.sales ?? 0);
    const bits = [g ? `${g} game${g === 1 ? "" : "s"} finished` : "", deals ? `${deals} deal${deals === 1 ? "" : "s"}` : ""].filter(Boolean);
    return `${s.says}: ${bits.join(", ") || "no record yet"}${s.counts_here ? "" : " (evidence only)"}`;
  });
  return [...lines, `Reach: ${c.reach.text} (unweighted)`];
}
