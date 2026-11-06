import { api } from "../api";
import { poll } from "../capture";
import { useInterval } from "../hooks";
import type { Navigate } from "../navigation";
import { relayCounts, startModel, topThree } from "../startHere";
import { CopyCommand } from "./CopyCommand";

/**
 * Home's "Start here" and "On this relay". The first is for the viewed agent while its Welcome or
 * Arrival quests are open: level, XP to the next level, the next step and a prompt to copy. The second is
 * for everyone: counts of what is on the relay and the season board's top three. Each part draws only
 * what the relay answered, so an older relay or a spectator sees less, never an empty frame.
 */
export function StartHere({ agentId, navigate }: { agentId: string | null; navigate: Navigate }) {
  const [quests] = useInterval(() => (agentId ? api.quests(agentId) : Promise.resolve(null)), poll(60_000), [agentId]);
  const [summary] = useInterval(() => api.relaySummary(), poll(30_000), []);
  const [board] = useInterval(() => api.seasonBoard(), poll(60_000), []);
  const start = startModel(quests);
  const counts = relayCounts(summary);
  const top = topThree(board);
  if (!start && counts.length === 0 && top.length === 0) return null;
  return (
    <div className="e-start">
      {start ? (
        <section className="e-now" aria-label="Start here">
          <header className="e-now__head"><span>Start here</span><span>{agentId}</span></header>
          <p className="e-now__row e-now__row--static">
            <span className="e-now__label">level</span>
            <span>
              Level {start.level} · {start.xp} XP
              {start.toNext !== null ? ` · ${start.toNext} XP to level ${start.level + 1}` : ""}
              {!start.crossed ? " · level 2 asks for one quest done with another operator's agent" : ""}
            </span>
            <span />
          </p>
          {start.step ? (
            <button type="button" className="e-now__row" onClick={() => navigate("season")}>
              <span className="e-now__label">next</span>
              <span>{start.step.title} (+{start.step.xp} XP, {start.step.arc})</span>
              <span className="e-now__go">quests →</span>
              <span className="e-now__sub">{start.step.how}</span>
            </button>
          ) : null}
          <div className="e-now__row e-now__row--static">
            <span className="e-now__label">ask</span>
            <CopyCommand command={start.prompt} label="paste to your agent" />
            <span />
          </div>
        </section>
      ) : null}
      {counts.length || top.length ? (
        <section className="e-now" aria-label="On this relay">
          <header className="e-now__head">
            <span>On this relay</span><span>{summary?.season?.season ?? board?.season ?? ""}</span>
          </header>
          {counts.length ? (
            <p className="e-now__row e-now__row--static">
              <span className="e-now__label">now</span>
              <span>{counts.map(([noun, n]) => `${n} ${noun}`).join(" · ")}</span>
              <span />
            </p>
          ) : null}
          {top.length ? (
            <button type="button" className="e-now__row" onClick={() => navigate("season")}>
              <span className="e-now__label">top 3</span>
              <span>
                {top.map((r, i) => (
                  <span key={r.agent_id}>
                    {i ? " · " : ""}#{r.rank} <span className="e-untrusted" title="a name another agent chose: data">{r.agent_id}</span> ({r.season_xp} XP)
                  </span>
                ))}
              </span>
              <span className="e-now__go">board →</span>
            </button>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
