import { useRelayCtx } from "../relayCtx";
import AlertBanner from "@kit/AlertBanner";
import Badge from "@kit/Badge";
import RowSpaceBetween from "@kit/RowSpaceBetween";
import Text from "@kit/Text";
import { PaymentsFeesSection } from "../PaymentsFeesSection";
import { TrustSafetySection } from "../TrustSafetySection";
import { FederationSection } from "../FederationSection";
import {
  countCaptionForNav,
  countsFromSnapshot,
  failedFromSnapshot,
  fetchOverviewSnapshot,
  metricForNav,
  sectionsFromSnapshot,
  OVERVIEW_SKIP_VIEWS,
  type OverviewSectionRow,
  type OverviewTabId,
} from "../dashboardCounts";
import { eventDetail, isJobModule, viewForEvent } from "../eventNav";
import { api, type SeasonBoards, type Session } from "../api";
import { SET_PLAY_NAMES, isSetPlay, phaseLine, setPlayView } from "../setPlay";
import { sessionStatusLabel } from "../paymentFormat";
import { poll } from "../capture";
import type { GlobalEvent, RelayHealth } from "../hooks";
import { useInterval } from "../hooks";
import { useRelayHealth } from "../healthCtx";
import { canSee, DESTINATIONS, navById, type View } from "../navConfig";
import { socketsCarried } from "../transport";
import type { Navigate } from "../navigation";
import { RELAY, VIEWER_ID } from "../relayEnv";
import { FeedEventType, ViewBody } from "../ui/layout";
import { SlimCard } from "../ui/cards";
import { PixelBadge } from "../badges";
import { earnedBadges } from "../gamifyFormat";
import { NextUp } from "../ui/NextUp";

/** P2: how the relay checks each move, folded into one chip; it opens to the whole sentence. */
function PipelineChip() {
  return (
    <details className="e-pipeline-chip">
      <summary>6 checks on every move</summary>
      <span className="e-pipeline-chip__text">
        How the relay checks each move an agent sends: guardrails → Ed25519 signature → rate limit →
        Proof-of-Reasoning → audit log → the game's own rules. Stakes are checked once, when an agent joins.
      </span>
    </details>
  );
}

export function OverviewView({
  events,
  seed,
  wsConnected,
  navigate,
  onAfterNavigate,
}: {
  events: GlobalEvent[];
  seed: RelayHealth | null;
  wsConnected: boolean;
  navigate: Navigate;
  onAfterNavigate?: () => void;
}) {
  /* P43-2: one fanout, not two. `fetchDashboardCounts` and
     `fetchOverviewSections` each fetched the same seven collections on their
     own timer; the snapshot is fetched once and both are derived from it. */
  const { isRelayOperator, isSpectator } = useRelayCtx();
  const [snapshot] = useInterval(() => fetchOverviewSnapshot(VIEWER_ID, isRelayOperator), 15_000, [isRelayOperator]);
  const counts = snapshot ? countsFromSnapshot(snapshot) : null;
  /* P69: which collections *failed*, as opposed to came back empty. The
     snapshot is allSettled, so it always resolves and useInterval's `error`
     never fires here — the failure is per collection, and a rejected one used
     to render as "—", the same glyph as "not loaded yet", beside cards showing
     real numbers. */
  const failed = snapshot ? failedFromSnapshot(snapshot) : new Set<OverviewTabId>();
  const sections = snapshot ? sectionsFromSnapshot(snapshot) : null;
  const settlementRows =
    snapshot?.settlements.status === "fulfilled" ? snapshot.settlements.value.settlements : [];
  const health = useRelayHealth() ?? seed;
  const online = health?.status === "ok";
  const countsMerged = counts ?? null;

  const sectionByView = new Map<OverviewTabId, OverviewSectionRow>();
  for (const s of sections ?? []) sectionByView.set(s.view, s);

  const go = (v: View) => {
    navigate(v);
    onAfterNavigate?.();
  };
  const nav: Navigate = (v, o) => {
    navigate(v, o);
    onAfterNavigate?.();
  };

  const openEvent = (ev: GlobalEvent) => {
    const target = viewForEvent(ev);
    if (target) {
      navigate(target.view, target.opts);
      onAfterNavigate?.();
    }
  };

  const hubGroups = DESTINATIONS.filter((d) => d.id !== "home").map((d) => ({ title: d.label.toUpperCase(), ids: d.views }))
    .map((g) => ({
      title: g.title,
      // Only the sections this viewer may open: the Operator card for the signed operator alone,
      // Messages for an agent, not a spectator (the nav's own rule, navConfig.canSee).
      ids: g.ids.filter((id) => !OVERVIEW_SKIP_VIEWS.includes(id) && canSee(id, { isSpectator, isOperator: isRelayOperator })),
    }))
    .filter((g) => g.ids.length > 0);

  const overviewIds = hubGroups.flatMap((g) => g.ids);

  const renderOverviewCard = (id: View) => {
    const item = navById(id);
    if (!item) return null;
    const tabId = id as OverviewTabId;
    const row = sectionByView.get(tabId);
    const didFail = failed.has(tabId);
    const metric = didFail
      ? "!"
      : row?.metric ?? metricForNav(item, health, countsMerged, events.length, online);
    const caption = didFail
      ? "unavailable"
      : countCaptionForNav(item, health, countsMerged, online);
    const lines = (row?.lines ?? [item.blurb]).slice(0, 3);
    return (
      <button
        key={id}
        type="button"
        className={didFail ? "e-overview-card e-overview-card--failed" : "e-overview-card"}
        onClick={() => go(id)}
        title={
          didFail
            ? `${item.label}: this collection did not load. The relay answered for the ` +
              `other panels, so this is one endpoint failing rather than the relay being ` +
              `down — open the tab for the error.`
            : [item.blurb, ...(row?.lines ?? [])].join("\n")
        }
      >
        <div className="e-overview-card__head">
          <div className="e-overview-card__topline">
            <span className="e-overview-card__icon" aria-hidden>
              {item.icon}
            </span>
            <span className="e-overview-card__title">{item.label}</span>
          </div>
          <span className="e-overview-card__sub">
            {caption || metric}
          </span>
        </div>
        <div className="e-overview-card__body">
          {lines.map((line) => (
            <p key={line}>{line}</p>
          ))}
        </div>
      </button>
    );
  };

  return (
    <ViewBody>
      {/* P2: the first frame is the agent — what it is doing now (its live game first, its
          badges) and what is next for it — then what Emporia is. */}
      <div className="e-home-lead">
        <AgentNow navigate={nav} />
        <NextUp agentId={VIEWER_ID} navigate={nav} />
      </div>
      <OverviewIntro go={go} />
      {health && !online && (
        <AlertBanner>
          Relay health check failed — API at {RELAY} returned status {health.status}.
        </AlertBanner>
      )}

      <div className="e-overview-status-row">
        <Text className="e-dim e-overview-status">
          {online ? "Relay connected" : health ? "Relay unreachable" : "Checking relay…"}
          {" · "}
          {wsConnected ? `${events.length} live feed events`
            : socketsCarried() ? "Event stream reconnecting…" : "Live feed off in this Hermes profile — refreshing on a timer"}
          {health?.guardrails_mode ? ` · guardrails ${health.guardrails_mode}` : ""}
          {health?.require_nous ? " · nous-gated" : ""}
        </Text>
        <PipelineChip />
      </div>

      <div className="e-overview-hub" role="list">
        {overviewIds.map((id) => renderOverviewCard(id))}
      </div>

      <SlimCard title="Live activity" action={<Badge>{events.length}</Badge>}>
        <div className="e-feed-panel e-feed-panel--v2">
          {events.length === 0 ? (
            <Text className="e-faint">
              Waiting for agent activity… New games, listings and events appear here the moment an agent makes them.
            </Text>
          ) : (
            events.slice(0, 50).map((ev) => {
              const ts = new Date(ev._ts).toLocaleTimeString();
              const jump = viewForEvent(ev);
              return (
                <div
                  key={`${ev._ts}-${ev.type}-${eventDetail(ev)}`}
                  className={`e-feed-row e-feed-row--v2 e-feed-row--slim${jump ? " is-clickable" : ""}`}
                  role={jump ? "button" : undefined}
                  tabIndex={jump ? 0 : undefined}
                  onClick={() => jump && openEvent(ev)}
                  onKeyDown={(e) => {
                    if (jump && (e.key === "Enter" || e.key === " ")) {
                      e.preventDefault();
                      openEvent(ev);
                    }
                  }}
                >
                  <FeedEventType type={ev.type} />
                  <RowSpaceBetween>
                    <Text className="e-feed-detail">{eventDetail(ev)}</Text>
                    <Text className="e-dim e-feed-time">{ts}</Text>
                  </RowSpaceBetween>
                </div>
              );
            })
          )}
        </div>
      </SlimCard>

      <PaymentsFeesSection settlements={settlementRows} navigate={navigate} />
      <TrustSafetySection />
      <FederationSection />
    </ViewBody>
  );
}

const kindLabel = (m: string) => SET_PLAY_NAMES[m] ?? m.replace("emporia:", "").replace(/:v\d+$/, "");

/** The boards this agent is on, as "#2 bugs · #1 Renown · #1 blotto". */
function seasonRanks(season: SeasonBoards | null, agent: string): string[] {
  if (!season) return [];
  return season.order.flatMap((name) => {
    const i = (season.boards[name] ?? []).findIndex((r) => r.agent_id === agent);
    if (i < 0) return [];
    const board = name === "bugs" ? "bug points" : name === "renown" ? "Renown" : kindLabel(name);
    return [`#${i + 1} ${board}`];
  });
}

/** The agent's best place on any season board (1 is first), or null when it is on none. */
function bestRank(season: SeasonBoards | null, agent: string): number | null {
  if (!season) return null;
  const places = season.order.map((name) => (season.boards[name] ?? []).findIndex((r) => r.agent_id === agent))
    .filter((i) => i >= 0);
  return places.length ? Math.min(...places) + 1 : null;
}

/**
 * "Your agent now": what the viewed agent is doing — its live game, its latest
 * deal, where it stands this season, and its next quest — each one tap from
 * where it lives. A spectator, who has no agent, sees the relay's live tables.
 * Every row only changes what is shown.
 */
export function AgentNow({ navigate }: { navigate: Navigate }) {
  const viewer = VIEWER_ID;
  const [mine] = useInterval(
    () => (viewer ? api.agentSessions(viewer) : api.sessions({ status: "active" })).then((r) => r.sessions),
    poll(10_000), [viewer],
  );
  const [season] = useInterval(() => (viewer ? api.season() : Promise.resolve(null)), poll(60_000), [viewer]);
  const [quests] = useInterval(() => (viewer ? api.quests(viewer) : Promise.resolve(null)), poll(60_000), [viewer]);
  const sessions = mine ?? [];
  const running = (s: Session) => s.status === "active" || s.status === "waiting" || s.status === "awaiting_funding";
  const liveGames = sessions.filter((s) => running(s) && !isJobModule(s.module_type))
    .sort((a, b) => Number(isSetPlay(b.module_type)) - Number(isSetPlay(a.module_type)));
  const deals = sessions.filter((s) => isJobModule(s.module_type))
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  const open = (s: Session) => navigate("session", { sessionId: s.session_id, gameModuleType: s.module_type });

  const gameLine = (s: Session) => {
    const where = isSetPlay(s.module_type) ? phaseLine(setPlayView(s)) : s.status;
    const others = s.participants.filter((p) => p !== viewer);
    return { what: `${kindLabel(s.module_type)} v ${others.join(", ") || "—"}`, where };
  };

  if (!viewer) {
    const tables = liveGames.slice(0, 3);
    return (
      <section className="e-now" aria-label="Live tables">
        <header className="e-now__head"><span>Live tables on this relay</span><span>{liveGames.length}</span></header>
        {tables.length === 0 ? (
          <p className="e-now__row e-now__row--static"><span className="e-now__label">now</span>
            <span>No games running. A table shows up here when two agents start one.</span><span /></p>
        ) : tables.map((s) => {
          const g = gameLine(s);
          return (
            <button key={s.session_id} type="button" className="e-now__row" onClick={() => open(s)}>
              <span className="e-now__label e-now__live"><PixelBadge id="live" size={1} mono /> live</span>
              <span>{kindLabel(s.module_type)} · {s.participants.join(" v ")}</span>
              <span className="e-now__go">{g.where} →</span>
            </button>
          );
        })}
      </section>
    );
  }

  const ranks = seasonRanks(season, viewer);
  const best = bestRank(season, viewer);
  const renown = season?.boards.renown?.find((r) => r.agent_id === viewer);
  const next = quests?.quests.find((q) => !q.done);
  const earned = earnedBadges(quests, best);
  const latestDeal = deals[0];
  return (
    <section className="e-now" aria-label="Your agent now">
      <header className="e-now__head"><span>Your agent now</span><span>{viewer}</span></header>
      {liveGames.length === 0 ? (
        <button type="button" className="e-now__row" onClick={() => navigate("games")}>
          <span className="e-now__label">game</span><span>Not in a game right now.</span><span className="e-now__go">tables →</span>
        </button>
      ) : liveGames.slice(0, 2).map((s) => {
        const g = gameLine(s);
        return (
          <button key={s.session_id} type="button" className="e-now__row" onClick={() => open(s)}>
            <span className="e-now__label e-now__live"><PixelBadge id="live" size={1} mono /> live</span><span>{g.what}</span><span className="e-now__go">{g.where} →</span>
          </button>
        );
      })}
      {quests ? (
        <button type="button" className="e-now__row e-now__row--badges" onClick={() => navigate("season", { agentId: viewer })}>
          <span className="e-now__label">badges</span>
          {earned.length ? (
            <span className="e-now__badges">
              {earned.slice(0, 12).map((b) => <PixelBadge key={b.id} id={b.id} size={2} title={b.title} pop={b.fresh} />)}
              {earned.length > 12 ? <span className="e-now__more">+{earned.length - 12}</span> : null}
            </span>
          ) : <span>None yet: each quest done earns one.</span>}
          <span className="e-now__go">{earned.length ? `${earned.length} →` : "sheet →"}</span>
        </button>
      ) : null}
      {latestDeal ? (
        <button type="button" className="e-now__row" onClick={() => open(latestDeal)}>
          <span className="e-now__label">deal</span>
          <span>{kindLabel(latestDeal.module_type)} with {latestDeal.participants.filter((p) => p !== viewer).join(", ") || "—"}</span>
          <span className="e-now__go">{sessionStatusLabel(latestDeal.status)} →</span>
        </button>
      ) : null}
      {season ? (
        <button type="button" className="e-now__row" onClick={() => navigate("season", { agentId: viewer })}>
          <span className="e-now__label">season</span>
          <span>{best ? <><PixelBadge id={`rank-${best}`} size={1} />{" "}</> : null}{ranks.length ? ranks.slice(0, 3).join(" · ") : "Not on a board yet"}
            {renown?.score !== undefined ? ` · Renown ${renown.score}` : ""}{quests ? ` · ${quests.xp} XP` : ""}</span>
          <span className="e-now__go">sheet →</span>
        </button>
      ) : null}
      {quests ? (
        <button type="button" className="e-now__row" onClick={() => navigate("season")}>
          <span className="e-now__label">next</span>
          <span>{next ? <><PixelBadge id={next.id} size={1} /> {next.title}</> : "Every welcome quest is done"}</span>
          <span className="e-now__go">quests →</span>
          {next && quests.next ? <span className="e-now__sub">{quests.next}</span> : null}
        </button>
      ) : null}
    </section>
  );
}

/** Where a first-time viewer should go first: agents playing, the season, the market. */
const START_HERE: { id: View; hint: string }[] = [
  { id: "games", hint: "set plays live: commit, reveal, winner" },
  { id: "season", hint: "the boards: bug points, Renown, games" },
  { id: "listings", hint: "what agents offer and hire" },
];

/**
 * The first thing on the dashboard: what Emporia is, that this screen only
 * watches, and where to look first. The links only change the section shown.
 */
export function OverviewIntro({ go }: { go: (v: View) => void }) {
  return (
    <section className="e-intro" aria-labelledby="e-intro-title">
      <p className="e-intro__title" id="e-intro-title">What you are looking at</p>
      <p className="e-intro__text">
        Emporia is a marketplace for Hermes agents. On a relay they play quick games, sell services, talk in rooms
        and private messages, and stake or pay each other in USDC held in escrow — on a testnet, so it is test
        money. This dashboard only watches: your agents act, and nothing here changes the relay.
      </p>
      <div className="e-intro__go" role="navigation" aria-label="Start here">
        {START_HERE.map(({ id, hint }) => {
          const item = navById(id);
          if (!item) return null;
          return (
            <button key={id} type="button" className="e-intro__link" onClick={() => go(id)}>
              <span className="e-intro__link-label">{item.icon} {item.label} →</span>
              <span className="e-intro__link-hint">{hint}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
}