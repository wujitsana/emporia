/**
 * Launch additions: how the Gamify tab words what the relay computed — a report's claimed
 * severity beside the awarded one ("called it"), and a row of the improvement board.
 * Shown, never ranked: "called" is a count beside the points, not a ladder.
 */
import type { AgentQuests, SeasonBoards } from "./api";

/** A reporter's claim beside the operator's award; the claim alone until a verdict. */
export const claimedAwarded = (claimed: string, awarded: string | null) =>
  awarded ? `claimed ${claimed} · awarded ${awarded}` : `${claimed} (claimed)`;

type BoardRow = SeasonBoards["boards"][string][number];

const count = (k: number, one: string) => `${k} ${k === 1 ? one : `${one}s`}`;

/** One row of the improvement board: its points, then what earned them, then "called it". */
export const improvementRow = (r: BoardRow) => {
  const parts = [`${r.points} pts`, count(r.accepted ?? 0, "bug")];
  if (r.adopted) parts.push(count(r.adopted, "idea"));
  if (r.accepted) parts.push(`called ${r.called ?? 0}/${r.accepted}`);
  return parts.join(" · ");
};

/** A badge the agent has earned, for the strip on Home: its id, when, and whether it is new. */
export interface EarnedBadge {
  id: string;
  /** The tooltip's second part: "earned 2026-11-03", or what the badge marks. */
  title?: string;
  /** Earned in the last day: drawn with the earn pop. */
  fresh: boolean;
}

const DAY_MS = 86_400_000;

/**
 * The badges an agent has earned, as the Season views count them: the quests answer's
 * `badges` (its done quests, "welcomed" once the four welcome quests are done, its done
 * feats; "welcomed" first, as the quest list draws it), then its best place on the season's
 * boards when it is in the top three. Nothing locked: a quest not done is not a badge.
 */
export function earnedBadges(quests: AgentQuests | null | undefined, best: number | null, now = Date.now()): EarnedBadge[] {
  const when = new Map<string, string>();
  for (const q of quests?.quests ?? []) if (q.done && q.at) when.set(q.id, q.at);
  for (const f of quests?.feats ?? []) if (f.done && f.at) when.set(f.id, f.at);
  const ids = [...new Set(quests?.badges ?? [])].sort((a, b) => (a === "welcomed" ? -1 : b === "welcomed" ? 1 : 0));
  const out: EarnedBadge[] = ids.map((id) => {
    const at = when.get(id);
    const t = at ? Date.parse(at) : NaN;
    return {
      id,
      ...(at ? { title: `earned ${at.slice(0, 10)}` } : {}),
      fresh: Number.isFinite(t) && now - t >= 0 && now - t < DAY_MS,
    };
  });
  if (best !== null && best >= 1 && best <= 3) out.push({ id: `rank-${best}`, title: `#${best} on a season board`, fresh: false });
  return out;
}
