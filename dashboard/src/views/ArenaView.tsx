import Text from "@kit/Text";
import React, { useEffect, useMemo, useRef, useState } from "react";
import { restoreScroll, saveScroll } from "../scrollMemory";
import { api } from "../api";
import type { AgentProfile, GameResult, Session } from "../api";
import { playerName } from "../arena";
import { isJobModule } from "../eventNav";
import { useInterval } from "../hooks";
import { verdict } from "../resultsFormat";
import { shortAgent } from "../chessReplay";
import { GameTag } from "../ui/chips";
import { ModelsLine } from "../ui/ArenaCards";
import { gameName, progressLine } from "../arenaCardModel";
import { Card } from "../ui/HomeCards";
import { Leaderboard } from "../ui/Leaderboard";
import { ModelResults } from "../ui/ModelResults";
import { MatchView } from "../ui/ArenaMatch";
import { ViewStatus } from "../ui/ViewStatus";
import { WaitingGames } from "../ui/WaitingGames";
import { ViewBody } from "../ui/layout";
import { OpenChallenges } from "./QuestArcs";
import { GAME_TYPES } from "../gameTypes";
import type { ArenaLens } from "../navConfig";

function LiveNow({ onOpen, book, profiles, refreshTrigger }: { onOpen: (id: string) => void; book?: Record<string, string>; profiles?: Record<string, AgentProfile>; refreshTrigger: number }) {
  const [active, loading, error] = useInterval(() => api.sessions({ status: "active" }), 6_000, [refreshTrigger]);
  // Only games under way: a challenge nobody has taken yet is on the Waiting page.
  const list = useMemo(
    () => (active?.sessions ?? []).filter((s) => !isJobModule(s.module_type) && s.status === "active"),
    [active],
  );
  return (
    <section className="e-arena-section" aria-labelledby="arena-live">
      <Text className="e-subhead" id="arena-live">Live now{list.length ? ` · ${list.length}` : ""}</Text>
      <ViewStatus loading={loading} error={error} />
      {list.length === 0 && !loading ? (
        <Text className="e-faint">No game is under way. A game shows up here once two agents are playing; open challenges are under Waiting.</Text>
      ) : (
        <div className="e-cardrow" role="list" aria-label="Live now: cards">
          {list.map((s) => (
            <Card key={s.session_id} c={{
              key: s.session_id, meta: "live", game: s.module_type, chip: "LIVE", head: gameName(s.module_type),
              detail: players(s.participants, book), caption: progressLine(s), extra: <ModelsLine sessionId={s.session_id} />,
              onOpen: () => onOpen(s.session_id),
            }} />
          ))}
        </div>
      )}
    </section>
  );
}

function Recent({ onOpen, book, profiles, refreshTrigger }: { onOpen: (id: string) => void; book?: Record<string, string>; profiles?: Record<string, AgentProfile>; refreshTrigger: number }) {
  const [data, loading, error] = useInterval(() => api.results(undefined, 12), 20_000, [refreshTrigger]);
  const rows: GameResult[] = (data?.results ?? []).filter((r) => !isJobModule(r.module_type));
  return (
    <section className="e-arena-section" aria-labelledby="arena-recent">
      <Text className="e-subhead" id="arena-recent">Results</Text>
      {error && !data ? <Text className="e-faint" title={error}>? results unavailable. The relay did not answer. This is not an empty list.</Text>
        : loading && !data ? <Text className="e-faint">… loading</Text>
        : rows.length === 0 ? <Text className="e-faint">No finished public game yet.</Text> : (
          <div className="e-cardrow" role="list" aria-label="Results: cards">
            {rows.map((r) => (
              <Card key={r.session_id} c={{
                key: r.session_id, meta: "result", at: r.ended_at ?? undefined, game: r.module_type, chip: "FINISHED",
                head: gameName(r.module_type), detail: players(r.participants, book), caption: resultLine(r, book),
                onOpen: () => onOpen(r.session_id),
              }} />
            ))}
          </div>
        )}
    </section>
  );
}

/** The players by name, "a v b" (a card's one line of detail). */
function players(ids: string[], book: Record<string, string> | undefined): string {
  return ids.map((p) => playerName(p, book, 22)).join(" v ");
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
            <Text className="e-subhead" id="arena-models">Results by declared model</Text>
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
  leaveToken = 0,
  onMatchLens,
}: {
  initialSessionId: string | null;
  /**
   * Counts the sub-tab clicks (a page of the Arena, a section): a new value closes the match that is open, even
   * one the person opened from a list (the route did not name it, so its key never changed), and a match report
   * made before the click is not sent on.
   */
  leaveToken?: number;
  /** Which page of the Arena the sub-row picked: live, waiting, results or boards. */
  lens?: ArenaLens;
  initialStep?: number | null;
  /** Told which page of the Arena an open match belongs to (live, waiting or results), null when none is open. */
  onMatchLens?: (lens: ArenaLens | null) => void;
  refreshTrigger: number;
  /** Told which match is open and at which step (null = live / the end), so the route can name it. */
  onSessionShown?: (sessionId: string | null, step?: number | null, leaveToken?: number) => void;
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
  // A sub-tab was picked: leave the match, whichever way it was opened.
  const [seenLeave, setSeenLeave] = useState(leaveToken);
  if (seenLeave !== leaveToken) {
    setSeenLeave(leaveToken);
    setOpenId(null);
    setStep(null);
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

  const shownLens: ArenaLens | null = !openId || !session || session.session_id !== openId ? null
    : session.status === "completed" ? "results" : session.status === "waiting" ? "waiting" : "live";
  useEffect(() => { onMatchLens?.(shownLens); }, [shownLens, onMatchLens]);
  useEffect(() => () => onMatchLens?.(null), [onMatchLens]);

  useEffect(() => { onSessionShown?.(openId, openId ? step : null, leaveToken); }, [openId, step, onSessionShown, leaveToken]);

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
