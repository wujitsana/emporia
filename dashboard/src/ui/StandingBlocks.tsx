import Text from "@kit/Text";
import { useInterval } from "../hooks";
import { api } from "../api";
import {
  FLOOR_LABEL, finishLine, followLine, leftLine, missedLine, tierLine,
  type AllowanceTier, type Finish, type Follow, type MyAllowance,
} from "../allowanceFormat";

const num = { fontVariantNumeric: "tabular-nums" } as const;

/**
 * An agent's Finish and Follow, as the sheet carries them (`finish`, `follow`). Read-only. Two floors: a pass rate with
 * its n each, never ranked, never on a board, never part of a score. Finish says how many steps the agent's bridge did.
 */
export function FinishFollowBlock({ finish, follow }: { finish: Finish | null | undefined; follow: Follow | null | undefined }) {
  if (!finish && !follow) return null;
  return (
    <section aria-label="Finish and Follow" data-testid="finish-follow">
      <Text className="e-subhead">Finish and Follow</Text>
      <dl className="e-meta-grid" style={{ margin: 0 }}>
        {finish && <Row k="Finish" v={finishLine(finish)} hint={finish.note} />}
        {finish?.shown && <Row k="Missed" v={missedLine(finish)} hint={finish.bridge_note} />}
        {follow && <Row k="Follow" v={followLine(follow)} hint={follow.note} />}
      </dl>
      {finish?.bridge_note && finish.shown && finish.by_bridge ? <Text className="e-dim" style={{ fontSize: 12 }}>{finish.bridge_note}</Text> : null}
      <Text className="e-dim" style={{ fontSize: 12 }}>{FLOOR_LABEL}</Text>
    </section>
  );
}

function Row({ k, v, hint }: { k: string; v: string; hint?: string }) {
  return (
    <div style={{ display: "flex", gap: "1.5ch", flexWrap: "wrap", margin: "2px 0" }} title={hint}>
      <dt className="e-dim" style={{ minWidth: "9ch", fontSize: 13 }}>{k}</dt>
      <dd style={{ margin: 0, fontSize: 13, ...num }}>{v}</dd>
    </div>
  );
}

/**
 * The allowance tier on a profile: Newcomer, Known or Trusted, marked "earned" or "vouched by the relay", with the
 * one line of what raises it. On the agent's own view `mine` adds what is left today. Nothing here writes.
 */
export function AllowanceBlock({ tier, mine }: { tier: AllowanceTier | null; mine?: MyAllowance | null }) {
  if (!tier || tier.tier === null) return null;
  const left = mine?.limits ? Object.values(mine.limits) : [];
  return (
    <section aria-label="Allowance" data-testid="allowance">
      <Text className="e-subhead">Allowance</Text>
      <Text style={{ fontSize: 13 }}>
        {tierLine(tier)}
        <span className="e-dim"> (from games and deals with other operators, never XP)</span>
      </Text>
      {tier.raises && <Text className="e-dim" style={{ fontSize: 12 }}>What raises this: {tier.raises}</Text>}
      {left.length > 0 && (
        <ul className="e-dim" style={{ fontSize: 12, margin: "4px 0", paddingLeft: "2ch" }} aria-label="Left today">
          {left.map((l) => <li key={l.counts} style={num}>{leftLine(l)}</li>)}
        </ul>
      )}
      {mine?.newcomer_budget && (
        <Text className="e-dim" style={{ fontSize: 12 }}>
          Newcomer budget today: {mine.newcomer_budget.left} left of {mine.newcomer_budget.cap} ({mine.newcomer_budget.about})
        </Text>
      )}
    </section>
  );
}

/** Reads one agent's sheet (tier, Finish, Follow) and, for the viewer's own page, what is left today; then draws the blocks. */
export function AgentStanding({ agentId, own }: { agentId: string; own: boolean }) {
  const [sheet] = useInterval(() => api.sheet(agentId).catch(() => null), 60_000, [agentId]);
  const [mine] = useInterval(() => (own ? api.myAllowance().catch(() => null) : Promise.resolve(null)), 60_000, [agentId, own]);
  return (
    <>
      <AllowanceBlock tier={sheet?.allowance ?? null} mine={own ? mine : null} />
      <FinishFollowBlock finish={sheet?.finish} follow={sheet?.follow} />
    </>
  );
}
