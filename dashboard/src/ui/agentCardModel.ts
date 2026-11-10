import React from "react";
import { api } from "../api";
import type { AgentProfile, AgentQuests, CharacterSheet } from "../api";
import { gamesLine, type AgentRecord } from "../settings";

/** What the agent card reads and says, apart from drawing it (AgentCard.tsx). */
export const LAB_QUESTS = ["lab-confirmed", "lab-fixed", "land-fix", "review-fix", "idea-adopted"];
export const COMPACT_BIO = 60;

export const say = (...parts: (string | number | null | undefined | false)[]) =>
  parts.filter((p) => p !== null && p !== undefined && p !== false && p !== "").join(" ");

/** The relay sends the day only ("today", "yesterday" or YYYY-MM-DD): shown as the day, never with a time. */
export function dayText(day: string): string {
  if (day === "today" || day === "yesterday") return day;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(day);
  return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])).toLocaleDateString(undefined, { timeZone: "UTC" }) : "";
}

export function declaredClient(a: Pick<AgentProfile, "client">): string {
  const c = a.client;
  return c?.name ? say(c.name, c.version) : "";
}

/** "Level 3 · 120 XP on relay r.example", from what the relay's quests answer says; "" when it said nothing. */
export function levelLine(q: AgentQuests | null | undefined): string {
  if (!q || !q.level || typeof q.level.level !== "number") return "";
  return say(`level ${q.level.level}`, typeof q.xp === "number" ? `· ${q.xp} XP` : "", q.relay_host ? `on relay ${q.relay_host}` : "");
}

export function recordSummary(r: AgentRecord | null | undefined, sheet: CharacterSheet | null | undefined,
                              q: AgentQuests | null | undefined): [string, string][] {
  const rows: [string, string][] = [];
  if (r?.games) {
    rows.push(["Games", gamesLine(r.games)]);
  }
  if (sheet?.pledges) rows.push(["Pledges", `${sheet.pledges.kept} kept · ${sheet.pledges.broken} broken`]);
  if (q?.quests) {
    const n = q.quests.filter((x) => x.done && LAB_QUESTS.includes(x.id)).length;
    if (n) rows.push(["Labs", `${n} lab report${n === 1 ? "" : "s"} credited`]);
  }
  return rows;
}

export function useAgentExtras(id: string, on: boolean) {
  const [q, setQ] = React.useState<AgentQuests | null>(null);
  const [sheet, setSheet] = React.useState<CharacterSheet | null>(null);
  const [rec, setRec] = React.useState<AgentRecord | null>(null);
  React.useEffect(() => {
    if (!on) return;
    let live = true;
    Promise.resolve().then(() => api.quests(id)).then((r) => live && setQ(r)).catch(() => live && setQ(null));
    return () => { live = false; };
  }, [id, on]);
  return { q, sheet, rec, setSheet, setRec };
}
