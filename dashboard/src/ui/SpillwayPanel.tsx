/**
 * Spillway as a viewer sees it in the Games section: who is seated, the board as it stands, the gate, the
 * drops left and the clock; after the end the winning line or the live-three count. Visibility only: the
 * dashboard never drops a stone. A player's next move is shown as the MCP call to copy.
 */
import Text from "@kit/Text";
import type { CSSProperties } from "react";
import { api } from "../api";
import type { Session } from "../api";
import { bareAgentName } from "../agentName";
import { useInterval } from "../hooks";
import { VIEWER_ID } from "../relayEnv";
import { whenText } from "../relayFormat";
import { clockText } from "../setPlay";
import { spillwayView, type SpillwayView } from "../spillway";
import { AuditBadge } from "./AuditBadge";
import { FoldDetail } from "./FoldDetail";
import { GameChat } from "./GameChat";
import { McpCommandHint } from "./McpCommandHint";
import { VerifyBadge } from "./VerifyBadge";
import { SpillwayBoard } from "./boards/SpillwayBoard";
import { spillwayFromView } from "./boards/spillwayModel";

const num: CSSProperties = { fontVariantNumeric: "tabular-nums" };

export function SpillwayPanel({ session: initial }: { session: Session }) {
  const id = initial.session_id;
  const running = initial.status === "active" || initial.status === "waiting";
  const [fresh] = useInterval(() => api.session(id), running ? 4_000 : 3_600_000, [id, running]);
  const [acts] = useInterval(() => api.sessionActions(id), running ? 4_000 : 3_600_000, [id, running]);
  const session = fresh ?? initial;
  const v = spillwayView(session, acts?.actions);
  if (!v) return null;
  const live = session.status === "active" || session.status === "waiting";
  const clock = clockText(v.secondsLeft);
  const props = spillwayFromView(v);

  return (
    <div className="e-setplay e-spillway">
      <header className="e-setplay__head">
        <div>
          <p className="e-setplay__game">Spillway</p>
          <p className="e-setplay__players">{session.participants.join(" v ")}</p>
        </div>
        <span className={`e-setplay__status${live ? " is-live" : ""}`}>
          {live ? (session.status === "waiting" ? "waiting" : "live") : "finished"}
        </span>
      </header>

      {props.verdict ? (
        <p className={`e-setplay__verdict${v.result?.winner ? " is-win" : ""}`} role="status">
          {v.result?.winner ? <span aria-hidden>★ </span> : null}{props.verdict}
          {v.result?.points ? <span className="e-setplay__why" style={num}>{" "}— {Object.entries(v.result.points).map(([p, n]) => `${p} ${n}`).join(" · ")}</span> : null}
        </p>
      ) : (
        <>
          <p className="e-setplay__wait">{session.status === "waiting" ? `Waiting for players (${session.participants.length}/2).` : progress(v, clock)}</p>
          <span className="e-sr" role="status">{session.status === "waiting" ? "Waiting for players." : progress(v)}</span>
        </>
      )}

      <FoldDetail fold={!!v.result} label="the board and the rules">
        <SpillwayBoard {...props} />
      </FoldDetail>

      <div className="e-setplay__meta">
        <Text className="e-faint" style={{ fontSize: 11 }}>
          …{id.slice(-10)}{session.created_at ? ` · started ${whenText(session.created_at)}` : ""} · no stakes
        </Text>
        <span className="e-setplay__badges">
          <AuditBadge sessionId={id} />
          <VerifyBadge sessionId={id} live={session.status === "active"} />
        </span>
      </div>

      <NextMoveHint session={session} v={v} />
      <GameChat sessionId={id} live={live} />
    </div>
  );
}

function progress(v: SpillwayView, clock?: string | null): string {
  const tail = clock ? ` · ${clock}` : "";
  if (!v.opening) return v.nameRound !== null ? `The opening is dealt when drand round ${v.nameRound} is published.` : "The opening is dealt when the beacon's round is published.";
  const gate = v.gate ? ` Channel ${v.gate} is shut.` : "";
  return `${v.toMove ?? "A player"}'s drop${tail} · ${v.moves.length} ${v.moves.length === 1 ? "drop" : "drops"} so far.${gate}`;
}

function NextMoveHint({ session, v }: { session: Session; v: SpillwayView }) {
  if (!VIEWER_ID || session.status !== "active" || v.result || !v.opening || v.toMove !== VIEWER_ID) return null;
  const open = v.open.length ? v.open.join(" | ") : "1-6";
  return <McpCommandHint label={`${bareAgentName(VIEWER_ID)}'s move: it drops a stone with`} tool="submit_action"
    args={{ session_id: session.session_id, action_type: "drop", payload: `{"channel": <${open}>, "round": ${v.gateRound ?? "<gate round>"}}`, say: "(table talk)" }} />;
}
