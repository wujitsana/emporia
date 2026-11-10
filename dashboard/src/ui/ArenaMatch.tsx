import Text from "@kit/Text";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { AgentProfile, Session, SessionModels } from "../api";
import { AgentCard } from "./AgentCard";
import { buildTimeline, clampStep, modelStanding, playerName } from "../arena";
import { sessionStatusLabel } from "../paymentFormat";
import { whenText } from "../relayFormat";
import { SET_PLAY_NAMES } from "../setPlay";
import { ChessReplayPanel } from "../views/GamesView";
import { ArenaFrame } from "./ArenaFrames";
import { ChessTransport } from "./chessControls";
import { AuditBadge } from "./AuditBadge";
import { VerifyBadge } from "./VerifyBadge";
import { GameChat } from "./GameChat";
import { useLiveMatch } from "./useLiveMatch";

/** Playback speed: one step per this many ms. Fixed on purpose — the slider is for going somewhere. */
const PLAY_MS = 1200;

/** What each player declared, and — once the game is over — whether it was revealed, mismatched or missing. */
export function ModelReveal({ session, book }: { session: Session; book: Record<string, string> | undefined }) {
  const ended = session.status === "completed" || session.status === "cancelled";
  const [models, setModels] = useState<SessionModels | null>(null);
  useEffect(() => {
    let live = true;
    api.sessionModels(session.session_id).then((m) => { if (live) setModels(m); }).catch(() => { if (live) setModels(null); });
    return () => { live = false; };
  }, [session.session_id, ended]);
  if (!models) return null;
  return (
    <div className="e-arena-models" aria-label="Declared models">
      <Text className="e-dim" style={{ fontSize: "0.68rem" }}>
        {models.ended ? "Model reveal" : "Declared models"} (declared by the players, never verified)
      </Text>
      <ul className="e-arena-slots">
        {session.participants.map((p) => (
          <li key={p} className="e-arena-slot">
            <span className="e-arena-slot__who">{playerName(p, book)}</span>
            <span className="e-faint" data-model-status={models.players[p]?.status ?? "none"}>
              {modelStanding(models.players[p], models.ended)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export interface MatchProps {
  session: Session;
  /** The step shown: a number, or null to follow the match live. Controlled by the Arena (and so by the address). */
  step: number | null;
  onStep: (step: number | null) => void;
  book?: Record<string, string>;
  /** The relay's cards for the players (compact AgentCards in the header). */
  profiles?: Record<string, AgentProfile>;
  onBack?: () => void;
}

/** One match: header, the step-through stage, the moves list, and the table talk beside them. */
export function MatchView({ session: initial, step, onStep, book, profiles, onBack }: MatchProps) {
  if (initial.module_type.includes("chess")) return <ChessMatch session={initial} step={step} onStep={onStep} book={book} profiles={profiles} onBack={onBack} />;
  return <GenericMatch session={initial} step={step} onStep={onStep} book={book} profiles={profiles} onBack={onBack} />;
}

function MatchHeader({ session, book, profiles, onBack, live, children }: {
  session: Session; book?: Record<string, string>; profiles?: Record<string, AgentProfile>; onBack?: () => void; live: boolean;
  children?: React.ReactNode;
}) {
  const name = SET_PLAY_NAMES[session.module_type] ?? session.module_type.replace("emporia:", "").replace(/:v\d+$/, "");
  return (
    <header className="e-arena-head">
      {onBack ? <button type="button" className="e-arena-back" onClick={onBack}>← Arena</button> : null}
      <div className="e-arena-head__row">
        <Text className="e-arena-head__title">{name}</Text>
        <Text className="e-dim e-status-txt">{live ? "live" : sessionStatusLabel(session.status)}</Text>
        <Text className="e-faint">…{session.session_id.slice(-8)}{session.created_at ? ` · started ${whenText(session.created_at)}` : ""}</Text>
      </div>
      <Text className="e-arena-head__players">
        {session.participants.map((p) => playerName(p, book, 24)).join("  v  ")}
      </Text>
      {session.participants.some((p) => profiles?.[p]) ? (
        <div className="e-arena-head__abouts" data-testid="arena-abouts">
          {session.participants.filter((p) => profiles?.[p]).map((p) => (
            <AgentCard key={p} agent={profiles![p]} variant="compact" level />
          ))}
        </div>
      ) : null}
      <div className="e-arena-head__badges">
        <AuditBadge sessionId={session.session_id} />
        <VerifyBadge sessionId={session.session_id} live={live} />
        {children}
      </div>
    </header>
  );
}

function ChessMatch({ session: initial, step, onStep, book, profiles, onBack }: MatchProps) {
  const { session } = useLiveMatch(initial);
  const live = session.status === "active";
  const report = useCallback((n: number, last: number) => onStep(n >= last && live ? null : n), [onStep, live]);
  return (
    <div className="e-arena-match">
      <MatchHeader session={session} book={book} profiles={profiles} onBack={onBack} live={live} />
      <ChessReplayPanel key={session.session_id} session={session} initialStep={step} onStepChange={report} book={book} />
      <ModelReveal session={session} book={book} />
    </div>
  );
}

function GenericMatch({ session: initial, step, onStep, book, profiles, onBack }: MatchProps) {
  const { session, actions, loaded, failed, wsLive } = useLiveMatch(initial);
  const live = session.status === "active" || session.status === "waiting";
  const timeline = useMemo(() => buildTimeline(session, actions), [session, actions]);
  const last = timeline.steps.length - 1;
  const explicit = clampStep(step, timeline.steps.length);
  const at = explicit ?? last;
  const [playing, setPlaying] = useState(false);
  const atRef = useRef(at);
  useEffect(() => { atRef.current = at; }, [at]);

  useEffect(() => {
    if (!playing) return;
    const t = setInterval(() => {
      if (atRef.current >= last) { setPlaying(false); return; }
      atRef.current += 1;
      onStep(atRef.current);
    }, PLAY_MS);
    return () => clearInterval(t);
  }, [playing, last, onStep]);

  const go = (n: number) => { setPlaying(false); onStep(Math.max(0, Math.min(last, n))); };
  const frame = timeline.frameAt(at);
  const stepInfo = timeline.steps[at];
  const following = explicit === null;

  return (
    <div className="e-arena-match" data-kind={timeline.kind}>
      <MatchHeader session={session} book={book} profiles={profiles} onBack={onBack} live={live} />
      <div className="e-arena-cols">
        <section className="e-arena-stage" aria-label="Game replay">
          {!loaded ? <Text className="e-faint">Loading the game…</Text> : failed && actions.length === 0 ? (
            <Text className="e-faint">The action log could not be read — it may be private to the players.</Text>
          ) : (
            <>
              <ChessTransport
                onPrev={() => go(at - 1)}
                onNext={() => go(at + 1)}
                onReplayToggle={() => {
                  if (playing) { setPlaying(false); return; }
                  if (at >= last) onStep(0);
                  setPlaying(true);
                }}
                replaying={playing}
                onLatest={() => { setPlaying(false); onStep(null); }}
                status={
                  <span className="e-chess-replay-status">
                    <span className="e-faint" role="status">
                      step {at} of {last}{live && following ? " · following live" : ""}{live && wsLive ? " ●" : ""}
                    </span>
                  </span>
                }
              />
              <input
                className="e-arena-slider"
                type="range"
                min={0}
                max={Math.max(0, last)}
                step={1}
                value={at}
                aria-label="Step"
                onChange={(e) => go(Number(e.target.value))}
              />
              <Text className="e-arena-stepline">
                <span className="e-faint">step {at}</span>{" "}
                {stepInfo.agent ? <span className="e-dim">{playerName(stepInfo.agent, book, 20)}</span> : null} {stepInfo.label}
              </Text>
              <ArenaFrame frame={frame} book={book} />
              {timeline.finished && at === last ? <ModelReveal session={session} book={book} /> : null}
            </>
          )}
        </section>
        <aside className="e-arena-side">
          <Text className="e-dim e-arena-side__label">moves · click to seek</Text>
          <ol className="e-arena-steps">
            {timeline.steps.slice(1).map((s) => (
              <li key={s.index}>
                <button type="button" className={`e-arena-step${s.index === at ? " is-now" : ""}`} onClick={() => go(s.index)}>
                  <span className="e-faint">{s.index}</span> {s.agent ? playerName(s.agent, book, 12) : ""} {s.label}
                </button>
              </li>
            ))}
          </ol>
          <GameChat sessionId={session.session_id} live={live} book={book} />
          {!(timeline.finished && at === last) ? <ModelReveal session={session} book={book} /> : null}
        </aside>
      </div>
    </div>
  );
}
