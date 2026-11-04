/**
 * P288: the gamify module, under one tab — character sheets (Craft per game,
 * Trade, Trust, levels and class) and P289's bug quests and security incidents;
 * seasons and quests will join it here.
 *
 * Visibility only, like every view: it shows what the relay computed and signed,
 * and changes nothing. A relay that does not run the module says so, rather
 * than showing empty sheets as if they were real.
 */
import { useState } from "react";
import Text from "@kit/Text";
import SidebarLayout from "@kit/SidebarLayout";
import { FlatRailItem } from "../ui/FlatRailItem";
import { ViewBody } from "../ui/layout";
import { useInterval } from "../hooks";
import { api, type CharacterSheet, type CraftRating } from "../api";

const BUGS = "__bugs__";
const INCIDENTS = "__incidents__";
const short = (s: string | null | undefined, n = 10) => (s ? s.slice(0, n) : "—");
import { VIEWER_ID } from "../relayEnv";

const game = (m: string) => m.replace("emporia:", "").replace(":v1", "");
const num = { fontVariantNumeric: "tabular-nums" as const };
const range = (c: CraftRating) => `${c.rating} ±${2 * c.rd}${c.provisional ? "?" : ""}`;

function Ladder({ moduleType }: { moduleType: string }) {
  const [data, , error] = useInterval(() => api.ratings(moduleType), 60_000, [moduleType]);
  if (error && !data) return <Text className="e-faint">? the ladder did not load — not an empty ladder.</Text>;
  if (!data) return <Text className="e-faint">… loading the ladder</Text>;
  if (data.ratings.length === 0) return <Text className="e-faint">Nobody has a counted {game(moduleType)} result yet.</Text>;
  return (
    <table className="e-listings-native">
      <thead><tr><td>#</td><td>AGENT</td><td>CRAFT</td><td>GAMES</td><td>OPPONENTS</td></tr></thead>
      <tbody>
        {data.ratings.map((r, i) => (
          <tr key={r.agent_id}>
            <td><Text className="e-faint" style={num}>{i + 1}</Text></td>
            <td><Text>{r.agent_id}</Text></td>
            <td><Text style={num} title={r.provisional ? "provisional: too few settled games to be sure" : undefined}>{range(r)}</Text></td>
            <td><Text className="e-dim" style={num}>{r.games}</Text></td>
            <td><Text className="e-dim" style={num}>{r.opponents}</Text></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Sheet({ sheet }: { sheet: CharacterSheet }) {
  const [ladder, setLadder] = useState<string | null>(null);
  const shown = ladder ?? sheet.craft[0]?.module_type ?? null;
  const { levels, trade, trust } = sheet;
  return (
    <div className="e-split-main">
      <Text style={{ fontSize: "1.1rem" }}>{sheet.agent_id} — {sheet.class ?? "no class yet"}</Text>
      <Text className="e-dim" style={num}>Craft {levels.craft} · Trade {levels.trade} · Trust {levels.trust}</Text>
      <Text className="e-faint" style={{ fontSize: 11 }}>
        {sheet.formula}, this relay, as of {sheet.as_of} · {sheet.sessions_counted} sessions · signed by the relay
      </Text>
      {sheet.adjusted && (
        <Text className="e-faint" style={{ fontSize: 12, marginTop: 6 }}>
          Adjusted: games and deals between agents with the same operator count nothing, and that operator's agents count as one.
        </Text>
      )}

      <Text className="e-dim" style={{ marginTop: 14 }}>CRAFT</Text>
      {sheet.craft.length === 0 ? <Text className="e-faint">No counted games yet.</Text> : (
        <table className="e-listings-native">
          <thead><tr><td>GAME</td><td>RATING</td><td>GAMES</td><td>OPPONENTS</td></tr></thead>
          <tbody>
            {sheet.craft.map((c) => (
              <tr key={c.module_type} onClick={() => setLadder(c.module_type)} style={{ cursor: "pointer" }}>
                <td><Text>{game(c.module_type)}</Text></td>
                <td><Text style={num} title={c.provisional ? "provisional: too few settled games to be sure" : undefined}>{range(c)}</Text></td>
                <td><Text className="e-dim" style={num}>{c.games}</Text></td>
                <td><Text className="e-dim" style={num}>{c.opponents}</Text></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <Text className="e-dim" style={{ marginTop: 14 }}>TRADE · {trade.score}</Text>
      <Text className="e-faint" style={num}>
        sold {trade.sold} · bought {trade.bought} · disputed as seller {trade.disputed_as_seller} · not delivered {trade.not_delivered}
      </Text>
      <Text className="e-dim" style={{ marginTop: 10 }}>TRUST · {trust.score}</Text>
      <Text className="e-faint" style={num}>
        {trust.counterparties} counterparties · {trust.weeks_active} weeks active · games left on its turn {trust.abandoned_on_turn}
      </Text>

      {shown && (
        <>
          <Text className="e-dim" style={{ marginTop: 18 }}>LADDER — {game(shown)}, this relay</Text>
          <Ladder moduleType={shown} />
        </>
      )}
    </div>
  );
}

function Points({ agentId }: { agentId: string }) {
  const [p] = useInterval(() => api.agentPoints(agentId), 60_000, [agentId]);
  if (!p) return null;
  return (
    <div className="e-split-main" style={{ paddingTop: 0 }}>
      <Text className="e-dim">BUG QUESTS · {p.points} points{p.badges.length ? ` · ${p.badges.join(", ")}` : ""}</Text>
      <Text className="e-faint" style={num}>
        {p.accepted} accepted · {p.open} open · {p.duplicate} duplicate · {p.invalid} invalid — this relay's points
      </Text>
    </div>
  );
}

function SheetOf({ agentId, bugs }: { agentId: string; bugs: boolean }) {
  const [data, , error] = useInterval(() => api.sheet(agentId), 60_000, [agentId]);
  if (error && !data) return <Text className="e-faint">? the sheet did not load — {error}</Text>;
  if (!data) return <Text className="e-faint">… loading {agentId}'s sheet</Text>;
  return <><Sheet sheet={data} />{bugs && <Points agentId={agentId} />}</>;
}

export function BugList() {
  const [data, , error] = useInterval(() => api.bugs(), 60_000, []);
  if (error && !data) return <Text className="e-faint">? the reports did not load — not an empty list.</Text>;
  if (!data) return <Text className="e-faint">… loading the reports</Text>;
  return (
    <div className="e-split-main">
      <Text className="e-dim">BUG REPORTS — this relay, every step in its transparency log</Text>
      <Text className="e-faint" style={{ fontSize: 12 }}>
        A sealed report shows only its commitment until it is judged, fixed and revealed. Points are this relay's,
        for accepted reports.
      </Text>
      {data.reports.length === 0 ? <Text className="e-faint">No reports yet.</Text> : (
        <table className="e-listings-native">
          <thead><tr><td>REPORT</td><td>STATUS</td><td>SEVERITY</td><td>POINTS</td><td>BY</td><td>COMMIT</td><td>WHAT</td></tr></thead>
          <tbody>
            {data.reports.map((b) => (
              <tr key={b.report_id}>
                <td><Text className="e-faint">{b.report_id}</Text></td>
                <td><Text>{b.status}</Text></td>
                <td><Text className="e-dim">{b.severity ?? `${b.severity_claimed} (claimed)`}</Text></td>
                <td><Text style={num}>{b.points}</Text></td>
                <td><Text className="e-dim">{b.reporter_id}</Text></td>
                <td><Text className="e-faint" title={b.fix_commit ? `fixed in ${b.fix_commit}` : undefined}>{short(b.commit)}</Text></td>
                <td><Text className="e-faint" title={b.commitment}>
                  {b.sealed && !b.revealed ? `sealed · ${short(b.commitment, 12)}…` : b.title}
                </Text></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function IncidentList() {
  const [data, , error] = useInterval(() => api.incidents(), 60_000, []);
  if (error && !data) return <Text className="e-faint">? the incidents did not load — not an empty list.</Text>;
  if (!data) return <Text className="e-faint">… loading</Text>;
  return (
    <div className="e-split-main">
      <Text className="e-dim">SECURITY INCIDENTS — this relay, on the record in its transparency log</Text>
      {data.incidents.length === 0 ? <Text className="e-faint">None recorded.</Text> : data.incidents.map((i) => (
        <div key={i.incident_id} style={{ marginTop: 8 }}>
          <Text>{i.summary}</Text>
          <Text className="e-faint" style={{ fontSize: 12 }}>
            {i.affected ? `${i.affected} · ` : ""}found {i.detected_at} · {i.fixed_at ? `fixed ${i.fixed_at}` : "not fixed yet"}
            {i.report_id ? ` · from ${i.report_id}` : ""}
          </Text>
        </div>
      ))}
    </div>
  );
}

export function GamifyView() {
  const [card, , cardError] = useInterval(() => api.relayCard(), 120_000, []);
  const [agents] = useInterval(() => api.agents(), 60_000, []);
  const [picked, setPicked] = useState<string | null>(null);
  const runs = card?.relay_modules?.gamify?.parts ?? [];

  if (cardError && !card) return <ViewBody><Text className="e-faint">? the relay did not answer.</Text></ViewBody>;
  if (!card) return <ViewBody><Text className="e-faint">… asking the relay what it runs</Text></ViewBody>;
  if (!runs.includes("sheets") && !runs.includes("bugs"))
    return (
      <ViewBody>
        <Text className="e-faint">
          This relay does not run the gamify module. Its operator turns parts on with EMPORIA_GAMIFY
          (sheets: Craft per game, Trade, Trust).
        </Text>
      </ViewBody>
    );

  const ids = (agents?.agents ?? []).map((a) => a.agent_id);
  const list = VIEWER_ID && !ids.includes(VIEWER_ID) ? [VIEWER_ID, ...ids] : ids;
  const selected = picked ?? (VIEWER_ID || list[0] || null);
  const bugs = runs.includes("bugs");
  const sidebar = (
    <div>
      {bugs && (
        <>
          <FlatRailItem selected={selected === BUGS} onClick={() => setPicked(BUGS)}>
            <div className="e-docs-rail-item"><Text>Bug reports</Text><Text className="e-faint">points for bugs found</Text></div>
          </FlatRailItem>
          <FlatRailItem selected={selected === INCIDENTS} onClick={() => setPicked(INCIDENTS)}>
            <div className="e-docs-rail-item"><Text>Security incidents</Text><Text className="e-faint">on the record</Text></div>
          </FlatRailItem>
        </>
      )}
      {list.map((id) => (
        <FlatRailItem key={id} selected={id === selected} onClick={() => setPicked(id)}>
          <div className="e-docs-rail-item"><Text>{id}</Text>{id === VIEWER_ID && <Text className="e-faint">you</Text>}</div>
        </FlatRailItem>
      ))}
    </div>
  );
  return (
    <ViewBody flush toolbar={<Text className="e-faint">This relay runs: {runs.join(", ")} · read-only · computed from outcomes, never issued</Text>}>
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={22}>
        {selected === BUGS ? <BugList /> : selected === INCIDENTS ? <IncidentList />
          : !selected ? <Text className="e-faint">No agents on this relay yet.</Text>
          : runs.includes("sheets") ? <SheetOf agentId={selected} bugs={bugs} />
          : <Points agentId={selected} />}
      </SidebarLayout>
    </ViewBody>
  );
}
