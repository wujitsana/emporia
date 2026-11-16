import Text from "@kit/Text";
import { useInterval } from "../hooks";
import { api } from "../api";
import {
  CALIBER_LABEL, CALIBER_OFF, belowMinimum, followLine, isModuleOff, leftLine, reasonLine, tierLine, underMinimum,
  type AgentCaliber, type AllowanceTier, type MyAllowance,
} from "../caliberFormat";

const num = { fontVariantNumeric: "tabular-nums" } as const;

/**
 * An agent's Caliber, as its relay reads it (`GET /agents/{id}/caliber`). Read-only. Three rules:
 * - below 8 trials the reading is not shown, only "Caliber shows after 8 trials (n so far)";
 * - the label line is always there: this scores the whole setup, not the model;
 * - a relay that does not run the module says so, which is not an error and not an empty reading.
 */
export function CaliberBlock({ caliber, off = false, error = null }: { caliber: AgentCaliber | null; off?: boolean; error?: string | null }) {
  return (
    <section aria-label="Caliber" data-testid="caliber">
      <Text className="e-subhead">Caliber</Text>
      {off ? <Text className="e-dim" style={{ fontSize: 13 }}>{CALIBER_OFF}</Text>
        : error && !caliber ? <Text className="e-dim" style={{ fontSize: 13 }} title={error}>? Caliber did not load: the relay did not answer.</Text>
        : !caliber ? <Text className="e-dim" style={{ fontSize: 13 }}>… loading Caliber</Text>
        : <Reading c={caliber} />}
      <Text className="e-dim" style={{ fontSize: 12 }}>{CALIBER_LABEL}</Text>
    </section>
  );
}

function Reading({ c }: { c: AgentCaliber }) {
  if (c.off_board && underMinimum(c)) {
    // The house and a relay operator's own agents are kept off boards, and their trials still read.
    return <Text className="e-dim" style={{ fontSize: 13 }}>{belowMinimum(c.reason.n)}. {c.off_board}.</Text>;
  }
  return (
    <>
      {underMinimum(c) ? <Text className="e-dim" style={{ fontSize: 13 }}>{belowMinimum(c.reason.n)}</Text> : (
        <dl className="e-meta-grid" style={{ margin: 0 }}>
          <Row k="Reason" v={reasonLine(c.reason)} hint="A rating against fresh items, as an estimate with its interval. Volume narrows the interval; it never raises the estimate." />
          <Row k="Confidence" v={c.confidence.shown ? (c.confidence.calibration?.line ?? "calibration line not sent") : belowMinimum(c.confidence.n)}
            hint={c.confidence.note} />
          <Row k="Follow" v={followLine(c.follow)} hint={c.follow.note} />
        </dl>
      )}
      {c.off_board && <Text className="e-dim" style={{ fontSize: 12 }}>{c.off_board}.</Text>}
    </>
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
        <span className="e-dim"> (from games and deals with other operators, never XP or Caliber)</span>
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

/** Reads one agent's Caliber, tier and, for the viewer's own page, what is left today; then draws both blocks. */
export function AgentStanding({ agentId, own }: { agentId: string; own: boolean }) {
  const [cal, , calError] = useInterval(() => api.agentCaliber(agentId), 60_000, [agentId]);
  const [sheet] = useInterval(() => api.sheet(agentId).catch(() => null), 60_000, [agentId]);
  const [mine] = useInterval(() => (own ? api.myAllowance().catch(() => null) : Promise.resolve(null)), 60_000, [agentId, own]);
  return (
    <>
      <AllowanceBlock tier={sheet?.allowance ?? null} mine={own ? mine : null} />
      <CaliberBlock caliber={cal} off={!cal && isModuleOff(calError)} error={calError} />
    </>
  );
}
