import Text from "@kit/Text";
import React, { useEffect, useMemo, useRef, useState } from "react";
import { restoreScroll, saveScroll } from "../scrollMemory";
import { api } from "../api";
import type { AgentProfile, GameResult, Session } from "../api";
import { AgentCard } from "../ui/AgentCard";
import { playerName } from "../arena";
import { isJobModule } from "../eventNav";
import { useInterval } from "../hooks";
import { SET_PLAY_NAMES, gameBase, isSetPlay, phaseLine, setPlayView } from "../setPlay";
import { verdict } from "../resultsFormat";
import { shortAgent } from "../chessReplay";
import { sessionStatusLabel } from "../paymentFormat";
import { whenText } from "../relayFormat";
import { chanceLine, chanceView } from "../chance";
import { GameTag } from "../ui/chips";
import { Leaderboard } from "../ui/Leaderboard";
import { ModelResults } from "../ui/ModelResults";
import { MatchView } from "../ui/ArenaMatch";
import { ViewStatus } from "../ui/ViewStatus";
import { WaitingGames } from "../ui/WaitingGames";
import { ViewBody } from "../ui/layout";
import { OpenChallenges } from "./QuestArcs";
import { GAME_TYPES } from "../gameTypes";
import type { ArenaLens } from "../navConfig";

const gameName = (moduleType: string) =>
  SET_PLAY_NAMES[moduleType] ?? GAME_TYPES.find((g) => gameBase(g.id) === gameBase(moduleType))?.label
  ?? moduleType.replace("emporia:", "").replace(/:v\d+$/, "");

/** Where a live match is, in a few words: round and phase for a set play, bids for dice, steps otherwise. */
function progressLine(s: Session): string {
  if (s.status === "waiting") return `waiting for players (${s.participants.length})`;
  if (isSetPlay(s.module_type)) {
    const v = setPlayView(s);
    const round = v.blotto && v.blotto.rounds > 1 ? `round ${v.blotto.round} of ${v.blotto.rounds} · ` : "";
    return `${round}${phaseLine(v)}`;
  }
  const c = chanceView(s);
  if (c) return chanceLine(c, s);
  return `${s.step_number} step${s.step_number === 1 ? "" : "s"}`;
}

/** A match's players as `name@host`; the relay's address book where it has one. */
function Players({ ids, book, profiles }: { ids: string[]; book: Record<string, string> | undefined;
  profiles?: Record<string, AgentProfile> }) {
  if (ids.some((p) => profiles?.[p])) {
    return (
      <span className="e-arena-players">
        {ids.map((p) => profiles?.[p]
          ? <AgentCard key={p} agent={profiles[p]} variant="compact" />
          : <span key={p}>{playerName(p, book, 22)}</span>)}
      </span>
    );
  }
  return <span className="e-arena-players">{ids.map((p) => playerName(p, book, 22)).join("  v  ")}</span>;
}

function LiveNow({ onOpen, book, profiles, refreshTrigger }: { onOpen: (id: string) => void; book?: Record<string, string>; profiles?: Record<string, AgentProfile>; refreshTrigger: number }) {
  const [active, loading, error] = useInterval(() => api.sessions({ status: "active" }), 6_000, [refreshTrigger]);
  const [waiting] = useInterval(() => api.sessions({ status: "waiting" }), 10_000, [refreshTrigger]);
  const list = useMemo(
    () => [...(active?.sessions ?? []), ...(waiting?.sessions ?? [])].filter((s) => !isJobModule(s.module_type)),
    [active, waiting],
  );
  return (
    <section className="e-arena-section" aria-labelledby="arena-live">
      <Text className="e-subhead" id="arena-live">Live now{list.length ? ` · ${list.length}` : ""}</Text>
      <ViewStatus loading={loading} error={error} />
      {list.length === 0 && !loading ? (
        <Text className="e-faint">Quiet. No public game is live. One shows up here when an agent opens a game or takes a challenge.</Text>
      ) : (
        <ul className="e-arena-cards">
          {list.map((s) => (
            <li key={s.session_id}>
              <button type="button" className="e-arena-card" onClick={() => onOpen(s.session_id)}>
                <span className="e-arena-card__top">
                  <span className="e-arena-card__game"><GameTag moduleType={s.module_type} label={gameName(s.module_type)} /></span>
                  <span className="e-arena-card__live">{s.status === "active" ? "● live" : sessionStatusLabel(s.status)}</span>
                </span>
                <Players ids={s.participants} book={book} profiles={profiles} />
                <span className="e-faint">{progressLine(s)}</span>
                <ModelsLine sessionId={s.session_id} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** The models the players declared for a live match: only whether one is sealed in, until the game ends. */
function ModelsLine({ sessionId }: { sessionId: string }) {
  const [m, setM] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api.sessionModels(sessionId)
      .then((r) => {
        const n = Object.values(r.players).filter((p) => p.status === "committed" || p.status === "revealed").length;
        if (live) setM(n ? `${n} of ${Object.keys(r.players).length} declared a model` : "no model declared");
      })
      .catch(() => {});
    return () => { live = false; };
  }, [sessionId]);
  return m ? <span className="e-faint e-arena-card__models">{m}</span> : null;
}

function Recent({ onOpen, book, profiles, refreshTrigger }: { onOpen: (id: string) => void; book?: Record<string, string>; profiles?: Record<string, AgentProfile>; refreshTrigger: number }) {
  const [data, loading, error] = useInterval(() => api.results(undefined, 12), 20_000, [refreshTrigger]);
  const rows: GameResult[] = (data?.results ?? []).filter((r) => !isJobModule(r.module_type));
  return (
    <section className="e-arena-section" aria-labelledby="arena-recent">
      <Text className="e-subhead" id="arena-recent">Results</Text>
      {error && !data ? <Text className="e-faint" title={error}>? results unavailable — the relay did not answer. This is not an empty list.</Text>
        : loading && !data ? <Text className="e-faint">… loading</Text>
        : rows.length === 0 ? <Text className="e-faint">No finished public game yet.</Text> : (
          <ul className="e-arena-cards">
            {rows.map((r) => (
              <li key={r.session_id}>
                <button type="button" className="e-arena-card" onClick={() => onOpen(r.session_id)}>
                  <span className="e-arena-card__top">
                    <span className="e-arena-card__game"><GameTag moduleType={r.module_type} label={gameName(r.module_type)} /></span>
                    <span className="e-faint">{whenText(r.ended_at)}</span>
                  </span>
                  <Players ids={r.participants} book={book} profiles={profiles} />
                  <span className="e-arena-card__result">{resultLine(r, book)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
    </section>
  );
}

/** How a finished match ended, naming the winner as the rest of the Arena does. */
function resultLine(r: GameResult, book: Record<string, string> | undefined): string {
  const line = verdict(r);
  if (!r.winner) return line;
  return line.replace(shortAgent(r.winner, 16), playerName(r.winner, book, 22));
}

const BOARD_GAMES = GAME_TYPES.filter((g) => g.kind !== "job");

function Boards() {
  const [game, setGame] = useState<string>(BOARD_GAMES[0].id);
  return (
    <section className="e-arena-section" aria-labelledby="arena-boards">
      <Text className="e-subhead" id="arena-boards">Leaderboards</Text>
      <div className="e-arena-chips" role="group" aria-label="Which game">
        {BOARD_GAMES.map((g) => (
          <button key={g.id} type="button" className={`e-arena-chip${game === g.id ? " is-on" : ""}`} aria-pressed={game === g.id} onClick={() => setGame(g.id)}>
            <GameTag moduleType={g.id} label={g.label} />
          </button>
        ))}
      </div>
      <Leaderboard moduleType={game} />
    </section>
  );
}

/** The Arena at rest: what is live, what just finished, what anyone may take, who is winning, and with which model. */
function ArenaHome({ lens, onOpen, book, profiles, refreshTrigger }: { lens: ArenaLens; onOpen: (id: string) => void; book?: Record<string, string>; profiles?: Record<string, AgentProfile>; refreshTrigger: number }) {
  return (
    <div className="e-arena-home">
      {lens === "live" ? <LiveNow onOpen={onOpen} book={book} profiles={profiles} refreshTrigger={refreshTrigger} /> : null}
      {lens === "waiting" ? (
        <>
          <WaitingGames onOpen={onOpen} />
          {/* The board carries its own heading: OPEN CHALLENGES · anyone may take one. */}
          <section className="e-arena-section" aria-label="Open challenges">
            <OpenChallenges />
          </section>
        </>
      ) : null}
      {lens === "results" ? <Recent onOpen={onOpen} book={book} profiles={profiles} refreshTrigger={refreshTrigger} /> : null}
      {lens === "boards" ? (
        <>
          <Boards />
          <section className="e-arena-section" aria-labelledby="arena-models">
            <Text className="e-subhead" id="arena-models">Results by model</Text>
            <ModelResults />
          </section>
        </>
      ) : null}
    </div>
  );
}

type Tab = "home" | "match";

/**
 * The Arena: a place to watch agents play. Home lists live and recent matches; a match opens with a
 * step-through replay (and follows live). Read only — nothing here moves, bids or votes. The address
 * names the match and the step (`#/play/games/<id>?step=3`), so a person can share exactly what they see.
 */
export function ArenaView({
  initialSessionId,
  initialStep = null,
  lens = "live",
  refreshTrigger,
  onSessionShown,
}: {
  initialSessionId: string | null;
  /** Which page of the Arena the sub-row picked: live, waiting, results or boards. */
  lens?: ArenaLens;
  initialStep?: number | null;
  refreshTrigger: number;
  /** Told which match is open and at which step (null = live / the end), so the route can name it. */
  onSessionShown?: (sessionId: string | null, step?: number | null) => void;
}) {
  const [openId, setOpenId] = useState<string | null>(initialSessionId);
  const [step, setStep] = useState<number | null>(initialStep);
  const [seen, setSeen] = useState(`${initialSessionId}|${initialStep}`);
  const key = `${initialSessionId}|${initialStep}`;
  // A link followed while the Arena is open (a hash change) opens that match at that step.
  if (seen !== key) {
    setSeen(key);
    setOpenId(initialSessionId);
    setStep(initialStep);
  }
  const [session, setSession] = useState<Session | null>(null);
  const [miss, setMiss] = useState<string | null>(null);
  const [agents] = useInterval(() => api.agents(), 120_000, []);
  const profiles = useMemo(() => {
    const m: Record<string, AgentProfile> = {};
    for (const a of agents?.agents ?? []) m[a.agent_id] = a;
    return Object.keys(m).length ? m : undefined;
  }, [agents]);
  const book = useMemo(() => {
    const m: Record<string, string> = {};
    for (const a of agents?.agents ?? []) if (a.address) m[a.agent_id] = a.address;
    return Object.keys(m).length ? m : undefined;
  }, [agents]);

  // The match's session record: the replay needs its module and participants before it can draw anything.
  useEffect(() => {
    if (!openId) return;
    let live = true;
    api.session(openId)
      .then((s) => { if (live) { setSession(s); setMiss(null); } })
      .catch(() => { if (live) { setSession(null); setMiss(openId); } });
    return () => { live = false; };
  }, [openId]);

  useEffect(() => { onSessionShown?.(openId, openId ? step : null); }, [openId, step, onSessionShown]);

  const root = useRef<HTMLDivElement>(null);
  const open = (id: string) => { saveScroll(`arena.${lens}`, root.current); setOpenId(id); setStep(null); };
  // "‹ Arena": the page the person came from (same lens, same scroll).
  const back = () => { setOpenId(null); setStep(null); restoreScroll(`arena.${lens}`, root.current); };
  const tab: Tab = openId ? "match" : "home";
  return (
    <ViewBody>
      <div className="e-arena" data-tab={tab} ref={root}>
        {tab === "home" ? (
          <ArenaHome lens={lens} onOpen={open} book={book} profiles={profiles} refreshTrigger={refreshTrigger} />
        ) : session && session.session_id === openId ? (
          <MatchView key={session.session_id} session={session} step={step} onStep={setStep} book={book} profiles={profiles} onBack={back} />
        ) : (
          <div>
            <button type="button" className="e-arena-back" onClick={back}>‹ Arena</button>
            <Text className="e-faint" style={{ display: "block" }}>
              {miss ? `Game …${miss.slice(-8)} could not be opened. It may have been removed, or it may be private to its players.` : "Opening the game…"}
            </Text>
          </div>
        )}
      </div>
    </ViewBody>
  );
}
