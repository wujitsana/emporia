import type { Session } from "./api";
import { isJobModule } from "./eventNav";
import { gameName } from "./attention";
import { bareAgentName } from "./agentName";
import { isSetPlay, phaseLine, setPlayView } from "./setPlay";

const running = (s: Session) => s.status === "active" || s.status === "waiting" || s.status === "awaiting_funding";

/**
 * The one line that says what an agent is doing: "alpha: blotto against beta, round 2, waiting on beta",
 * or "alpha is not in a game". The person is the reader, so the agent is named. `session` is the game
 * the line opens, when there is one.
 */
export function nowLine(agent: string, sessions: Session[]): { text: string; session: Session | null; live: boolean } {
  const games = sessions.filter((s) => running(s) && !isJobModule(s.module_type));
  const s = games.find((g) => isSetPlay(g.module_type)) ?? games[0];
  const name = bareAgentName(agent);
  if (!s) return { text: `${name} is not in a game`, session: null, live: false };
  const others = s.participants.filter((p) => p !== agent).map(bareAgentName);
  const against = others.length ? ` against ${others.join(", ")}` : "";
  let where: string = s.status === "waiting" ? "waiting for players" : "";
  if (isSetPlay(s.module_type)) {
    const v = setPlayView(s);
    where = phaseLine(v);
  } else if (!where) {
    where = `${s.step_number} step${s.step_number === 1 ? "" : "s"} in`;
  }
  return { text: `${name}: ${gameName(s.module_type)}${against}, ${where}`, session: s, live: true };
}
