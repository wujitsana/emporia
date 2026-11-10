import "./ui/kit/global.css";
import "./dashboard.css";

import Text from "@kit/Text";
import { RelayStrip } from "./RelayStrip";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { api } from "./api";
import { BottomBar, HeaderLinks, MobileNavDrawer, SectionTabs, SubRow } from "./AppNav";
import { NowStrip } from "./ui/NowStrip";
import { ThemeCompact } from "./ThemeControls";
import { ProfileView } from "./ProfileView";
import { RELAY } from "./relayEnv";
import { setOwnHost } from "./agentName";
import { RelayContextProvider } from "./relayContext";
import { useRelayCtx } from "./relayCtx";
import { ViewerSwitcher } from "./ui/ViewerSwitcher";
import { NotAnAgentNote } from "./ui/NotAnAgentNote";
import { hermesProfile, MODE } from "./transport";
import { readPref, writePref } from "./prefs";
import { DESKTOP_ROOT } from "./shadowScope";
import { RelayHealthCtx } from "./healthCtx";
import { applyTheme, nextTint, toggleTheme, THEMES, TINTS, type Theme, type Tint } from "./themeConfig";
import { useAgentWs, useGlobalEvents, useInterval } from "./hooks";
import { ARENA_LENSES, canSee, dealsAvailable, destById, destinationOf, navById, navForHotkey, siblingView, type ArenaLens, type Destination, type View } from "./navConfig";
import { GatedNote } from "./ui/GatedNote";
import { recall, recallOf, remember } from "./viewMemory";
import { isNarrow, useNarrow, useOverlayFocus } from "./ui/overlayFocus";
import { useNavLayout } from "./ui/navLayout";
import { PageHeading, ViewBoundary } from "./ui/layout";
import { isTyping } from "./keyTarget";
import { sessionSection } from "./eventNav";
import type { Navigate } from "./navigation";
import {
  OverviewView,
  LabsView,
  RoomsView,
  AgentsView,
  AgorasView,
  FeedView,
  MessagesView,
  GamesView,
  ArenaView,
  FeesView, StatsView, DocsView, SettingsView, RelayView, OperatorView, GamifyView,
} from "./views";
import { VIEWER_ID } from "./relayEnv";
import { currentHash, onRouteChange, routeFromHash, stepFromHash, syncHash } from "./dashboardRoute";
import { CAPTURE, PARAM_THEME, PARAM_TINT } from "./capture";
import { AnimaticHudFoot, BrandTitle } from "./ui/LookChrome";
import { AgentChatDrawer, ChatMenuItem, ChatToggle } from "./chat/AgentChat";
import { chatOpen, setChatOpen, subscribeChatOpen } from "./chat/chatAccess";
import { BadgeGallery } from "./badges/BadgeGallery";

const LS_THEME = "emporia_theme";
const LS_TINT = "emporia_tint";

// `?theme=` / `?tint=` win for this load (not saved); capture mode starts light, tint-green.
function readTheme(): Theme {
  if (PARAM_THEME) return PARAM_THEME;
  if (CAPTURE) return "theme-light";
  const v = readPref(LS_THEME);
  return (THEMES as readonly string[]).includes(v ?? "") ? (v as Theme) : "theme-light";
}
function readTint(): Tint {
  if (PARAM_TINT !== null) return PARAM_TINT;
  if (CAPTURE) return "tint-green";
  const v = readPref(LS_TINT);
  return (TINTS as readonly string[]).includes(v ?? "") ? (v as Tint) : "";
}

/** The sub-tab last seen in each destination in this browser tab (navForHotkey ignores a name it does not know). */
function recalledSections(): Partial<Record<Destination, View>> {
  const out: Partial<Record<Destination, View>> = {};
  for (const d of ["home", "agora", "arena", "labs", "agent"] as const) {
    const v = recall(`dest.${d}`);
    if (v) out[d] = v as View;
  }
  return out;
}

export default function App() {
  return (
    <RelayContextProvider>
      <AppInner />
    </RelayContextProvider>
  );
}

function AppInner() {
  const { isRelayOperator, isSpectator } = useRelayCtx();
  // P234: inside Hermes, a profile that is no agent shows nothing of the relay.
  const hermesNonAgent = hermesProfile()?.isAgent === false;
  const [view, setView] = useState<View>(() => routeFromHash().view);
  // The Arena's page (Live, Waiting, Results, Boards): the route's, `#/arena/waiting`.
  const [lens, setLens] = useState<ArenaLens>(() => routeFromHash().lens ?? "live");
  const [theme, setTheme] = useState<Theme>(readTheme);
  const [tint, setTint] = useState<Tint>(readTint);
  const [navOpen, setNavOpen] = useState(false);
  const shellRef = useRef<HTMLDivElement>(null);
  const drawerRef = useRef<HTMLElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  const narrow = useNarrow();
  // The navigation follows the dashboard's own width, not the window's (ui/navLayout.ts).
  const navLayout = useNavLayout(mainRef);
  const chatIsOpen = useSyncExternalStore(subscribeChatOpen, chatOpen);
  // The phone menu takes the focus while open and gives it back on close (ui/overlayFocus.ts).
  useOverlayFocus(navOpen, drawerRef);
  // The menu exists in the phone layout only: widening the frame closes it,
  // or the view would stay inert behind a menu that is no longer drawn.
  if (navOpen && navLayout !== "phone") setNavOpen(false);
  // What an overlay covers is out of reach of Tab and of a screen reader: the view under
  // the menu, and the view under full-page chat.
  const mainCovered = navOpen || (narrow && chatIsOpen);
  // P5: the chat open beside the view takes ~27rem of the header's row; the header then
  // drops what it can spare (the relay's host and version, the chat button's word) so it
  // stays on one row and nothing jumps when the chat slides in (dashboard.css, the look's § 11).
  const chatBeside = chatIsOpen && !narrow;

  // From the route at first render: a child effect (GamesView naming its session) runs before
  // App's own effects, so reading the hash later would read the one it just rewrote.
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(() => routeFromHash().sessionId);
  const [selectedStep, setSelectedStep] = useState<number | null>(() => stepFromHash());
  const [selectedRoomId, setSelectedRoomId] = useState<string | null>(null);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [gameModuleType, setGameModuleType] = useState<string | null>(null);

  const [sessionsRefresh, setSessionsRefresh] = useState(0);
  const [inboxUnread, setInboxUnread] = useState(0);

  const { events, seed, wsConnected } = useGlobalEvents();
  /* P43-2: the app's ONE /health poll. Three other components polled it
     independently — one of them while receiving this very value as a prop.
     They read `useRelayHealth()` now. */
  const [healthPoll] = useInterval(() => api.health(), 5_000);

  /* P38-1: the per-agent notification channel, opened once here so every view
     shares one connection (calling useAgentWs in each consumer would open a
     socket each). Everything the relay pushes to an agent — DMs, invites,
     session starts, kicks — arrives on it, and the dashboard had been polling
     for all of it. */
  const agentWs = useAgentWs(RELAY, VIEWER_ID);

  useEffect(() => {
    const viewer = VIEWER_ID;
    if (!viewer) return;
    const poll = () => api.agentInbox(viewer, true, 1).then((d) => setInboxUnread(d.unread_total ?? d.count)).catch(() => {});
    poll();
    // The interval is the safety net for a dropped socket now, not the way the
    // badge stays current — the `agentWs.last` dep is. Every event type the
    // relay sends on this channel also lands in the agent's inbox. With no
    // socket (a Hermes profile that cannot carry one, the Desktop Webapp) the
    // poll is all there is, so it runs more often.
    const t = setInterval(poll, agentWs.connected ? 60_000 : 15_000);
    return () => clearInterval(t);
  }, [agentWs.last, agentWs.connected]);
  const relayHealth = healthPoll ?? seed;
  // Agent names are bare on this relay and "name@host" only for visitors (agentName.ts).
  setOwnHost((relayHealth as { host?: string } | null)?.host);
  // Deals (You › Deals) is drawn only on a relay that can sell services: escrow on, or cards live.
  const dealsOn = dealsAvailable(relayHealth);

  /* P41-2: keyed on the newest event's identity rather than on
     `events[0]?._ts`. `_ts` is `Date.now()`, so two events arriving in the same
     millisecond produce the same key and the second one silently fails to
     trigger a refresh. useGlobalEvents prepends a fresh object per event, so
     identity changes exactly once per event and never collides — and it is a
     plain variable, which is what the dependency checker needs to see. */
  const latestEvent = events[0] ?? null;
  const [seenEvent, setSeenEvent] = useState(latestEvent);
  if (seenEvent !== latestEvent) {
    setSeenEvent(latestEvent);
    if (latestEvent) {
      if (latestEvent.type === "session_created" || latestEvent.type === "session_completed"
        || latestEvent.type === "action_result")
        setSessionsRefresh((n) => n + 1);
    }
  }

  // A change from the theme picker fades; one from the t/T hotkeys (or any in capture mode) is instant.
  const instantNext = useRef(false);
  useEffect(() => {
    applyTheme(theme, tint, { fade: !CAPTURE && !instantNext.current, capture: CAPTURE });
    instantNext.current = false;
  }, [theme, tint]);

  const setThemeAndApply = (t: Theme) => {
    setTheme(t);
    if (!CAPTURE) writePref(LS_THEME, t);
  };
  const setTintAndApply = (t: Tint) => {
    setTint(t);
    if (!CAPTURE) writePref(LS_TINT, t);
  };

  const applyView = useCallback((id: View, sessionId?: string | null) => {
    setView(id);
    syncHash(id, sessionId, null, lens);
  }, [lens]);

  // A route that names a session (`#/arena/games/<id>`) opens it — on load and on a
  // route change (a hash change; on the Desktop page, a palette command).
  useEffect(() => {
    const onHash = () => {
      const r = routeFromHash();
      setView(r.view);
      setLens(r.lens ?? "live");
      if (r.sessionId) setSelectedSessionId(r.sessionId);
      // Back to a page with no match named: close the match that was open.
      else if (r.view === "games") { setSelectedSessionId(null); setSelectedStep(null); }
      setSelectedStep(r.sessionId ? stepFromHash() : null);
      setNavOpen(false);
    };
    onHash();
    const off = onRouteChange(onHash);
    if (!currentHash()) syncHash(routeFromHash().view);
    return off;
  }, []);

  const goToView = useCallback(
    (id: View) => {
      applyView(id);
      setNavOpen(false);
      // A section picked from the menu over full-page chat is shown; beside the drawer it stays open.
      if (isNarrow()) setChatOpen(false);
      setGameModuleType(null);
      setSelectedSessionId(null);
      setSelectedStep(null);
    },
    [applyView],
  );

  // A lens of the Arena (Live, Waiting, Results, Boards): its own page; an open match is left.
  const goToLens = useCallback((l: ArenaLens) => {
    setView("games");
    setLens(l);
    setSelectedSessionId(null);
    setSelectedStep(null);
    setNavOpen(false);
    syncHash("games", null, null, l);
    remember("arena.lens", l);
  }, []);

  // Each destination reopens on the section last seen in it (Labs & Season → Season, if that was last);
  // kept for this browser tab, so a reload comes back to the same sub-tab.
  const lastInDest = useRef<Partial<Record<Destination, View>>>(recalledSections());
  useEffect(() => {
    const d = destinationOf(view).id;
    lastInDest.current[d] = view;
    remember(`dest.${d}`, view);
  }, [view]);
  const goToDest = useCallback(
    (id: Destination) => {
      const found = navForHotkey(destById(id)?.hotkey ?? "", { isSpectator, isOperator: isRelayOperator, dealsOn }, lastInDest.current);
      if (!found) return;
      // The Arena reopens on the page (Live, Waiting, Results, Boards) the person last had.
      if (found.id === "games") {
        const l = recallOf<ArenaLens>("arena.lens", ARENA_LENSES.map((x) => x.id), "live");
        if (l !== lens) { goToLens(l); return; }
      }
      goToView(found.id);
    },
    [goToView, goToLens, lens, isSpectator, isRelayOperator, dealsOn],
  );

  const navigate = useCallback<Navigate>(
    (toView, opts) => {
      if (toView === "session") {
        // A session lives in Games or Deals by its module; ask the relay once if the caller did not say.
        const id = opts?.sessionId;
        if (!id) return;
        setSelectedSessionId(id);
        setSelectedStep(null);
        if (opts?.gameModuleType) applyView(sessionSection(opts.gameModuleType), id);
        else api.session(id)
          .then((s) => applyView(sessionSection(s.module_type), id))
          .catch(() => applyView("games", id));   // Games says it could not be opened
        return;
      }
      applyView(toView, opts?.sessionId);
      if (opts?.sessionId !== undefined) setSelectedSessionId(opts.sessionId);
      if (opts?.roomId !== undefined) setSelectedRoomId(opts.roomId);
      if (opts?.agentId !== undefined) setSelectedAgentId(opts.agentId);
      if (opts?.gameModuleType !== undefined) setGameModuleType(opts.gameModuleType || null);
    },
    [applyView],
  );

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setNavOpen(false);
        return;
      }
      // In the Hermes tab (closed shadow root) e.target is the host; isTyping asks the app's own root.
      if (isTyping(e, shellRef.current) || e.metaKey || e.ctrlKey || e.altKey) return;
      // On the Desktop page the window is the whole app's: only keys pressed in this page count.
      // (A click on the page focuses its root, `.e-desktop-root`, so that is where its keys land.)
      if (MODE === "desktop" && !(e.target instanceof Element && e.target.closest(`.${DESKTOP_ROOT}`))) return;
      // t: next tint, T: light ↔ dark — instant, for a capture script on the beat.
      // Saved like the picker's choice, except in capture mode.
      if (e.key === "t" || e.key === "T") {
        instantNext.current = true;
        if (e.key === "t") setTint((cur) => {
          const next = nextTint(cur);
          if (!CAPTURE) writePref(LS_TINT, next);
          return next;
        });
        else setTheme((cur) => {
          const next = toggleTheme(cur);
          if (!CAPTURE) writePref(LS_THEME, next);
          return next;
        });
        return;
      }
      // `[` / `]`: the previous / next section in this destination (Feed ↔ Agora ↔ Rooms ↔ Messages).
      if (e.key === "[" || e.key === "]") {
        const next = siblingView(view, e.key === "]" ? 1 : -1, { isSpectator, isOperator: isRelayOperator, dealsOn });
        if (next !== view) goToView(next);
        return;
      }
      // P35-2: `navForHotkey` applies the same visibility rule the rendered
      // nav does. Searching the unfiltered NAV here is what let a spectator
      // reach the two sections the nav hides.
      const found = navForHotkey(e.key, { isSpectator, isOperator: isRelayOperator, dealsOn }, lastInDest.current);
      if (found) goToView(found.id);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
    // `isSpectator` belongs here: the handler closes over it, and it stops being
    // constant the moment the dashboard/challenge login flow lands (the token
    // setter is already on the context).
  }, [goToView, isSpectator, isRelayOperator, dealsOn, view]);

  const currentNav = navById(view);
  const shown = canSee(view, { isSpectator, isOperator: isRelayOperator, dealsOn });
  // Picking a game or a deal names it in the route, so the address opens that session.
  const onSessionShown = useCallback((id: string | null, step?: number | null) => {
    if (view === "games" || view === "deals") syncHash(view, id, step, lens);
  }, [view, lens]);

  // After a page change from the tabs, the focus goes to the page title (and is read out), not left on the tab.
  const firstView = useRef(true);
  useEffect(() => {
    if (firstView.current) { firstView.current = false; return; }
    const el = shellRef.current?.querySelector<HTMLElement>("#e-page-title");
    // Only when the focus was on a tab or nothing: never steal it from a field someone is typing in.
    const active = shellRef.current?.getRootNode() instanceof ShadowRoot
      ? (shellRef.current.getRootNode() as ShadowRoot).activeElement : document.activeElement;
    if (el && (!active || active === document.body || active.closest?.("nav, .e-bottom-bar"))) el.focus({ preventScroll: true });
  }, [view]);

  return (
    <RelayHealthCtx.Provider value={relayHealth}>
    <div ref={shellRef} data-nav={navLayout} className={`e-app-shell e-dashboard-shell e-app-shell--header-nav${navOpen ? " is-nav-open" : ""}${chatBeside ? " has-chat" : ""}`}>
        <aside ref={drawerRef} className="e-app-drawer" aria-label="Menu" inert={!navOpen}>
          <MobileNavDrawer
            view={view}
            onGoDest={goToDest}
            onGoView={goToView}
            onClose={() => setNavOpen(false)}
            theme={theme}
            tint={tint}
            onTheme={setThemeAndApply}
            onTint={setTintAndApply}
            badges={inboxUnread > 0 ? { messages: inboxUnread } : undefined}
            lead={<ChatMenuItem onOpen={() => setNavOpen(false)} />}
          />
        </aside>
        <button
          type="button"
          className="e-app-backdrop"
          aria-label="Close menu"
          onClick={() => setNavOpen(false)}
        />
        <main ref={mainRef} className="e-app-main" inert={mainCovered}>
          {/* Rail layout only (CSS): the five places as icons down the left edge. */}
          <SectionTabs
            view={view}
            onGoDest={goToDest}
            variant="rail"
            badges={inboxUnread > 0 ? { messages: inboxUnread } : undefined}
          />
          <div className="e-app-col">
          <header className="e-app-top">
            <div className="e-app-top__row">
              <Text className="e-brand-title e-brand-title--header"><BrandTitle replay={view} /></Text>
              <SectionTabs
                view={view}
                onGoDest={goToDest}
                variant="horizontal"
                short={navLayout === "medium"}
                badges={inboxUnread > 0 ? { messages: inboxUnread } : undefined}
              />
              <div className="e-app-top__actions">
                <ThemeCompact
                  theme={theme}
                  tint={tint}
                  onTheme={setThemeAndApply}
                  onTint={setTintAndApply}
                />
                <ViewerSwitcher />
                <HeaderLinks view={view} onGo={goToView} />
                <ChatToggle compact={chatBeside} />
                <RelayStrip
                  relayUrl={RELAY}
                  wsConnected={wsConnected}
                  health={relayHealth}
                />
              </div>
            </div>
          </header>
          {/* Phone: one row — the menu (sections, viewer, theme), where you are, and the relay's state. */}
          <div className="e-mobile-top">
            <button
              type="button"
              className="e-menu-button"
              onClick={() => setNavOpen(true)}
              aria-expanded={navOpen}
              aria-label="Open menu"
            >
              <span aria-hidden>☰</span>
              {inboxUnread > 0 && <span className="e-nav-badge">{inboxUnread > 99 ? "99+" : inboxUnread}</span>}
            </button>
            <span className="e-mobile-top__where">
              <Text className="e-brand-title"><BrandTitle replay={view} /></Text>
              {currentNav && <span className="e-mobile-top__section">{currentNav.label}</span>}
            </span>
            <ChatToggle compact />
            <RelayStrip
              relayUrl={RELAY}
              wsConnected={wsConnected}
              health={relayHealth}
              compact
            />
          </div>
          {/* The pages inside this place (Feed · Topics · Rooms · Agents), one strip, on every width and every place. */}
          <SubRow
            view={view}
            lens={lens}
            badges={inboxUnread > 0 ? { messages: inboxUnread } : undefined}
            onGoView={goToView}
            onGoLens={goToLens}
          />
          {!hermesNonAgent && VIEWER_ID && view !== "overview" ? <NowStrip navigate={navigate} /> : null}
          <div className="e-view">
            <NotAnAgentNote />
            {!hermesNonAgent && <PageHeading item={currentNav} agent={VIEWER_ID} />}
            {/* P234: a Hermes profile that is no agent shows nothing of the relay. */}
            {!hermesNonAgent && !shown && <GatedNote view={view} />}
            {!hermesNonAgent && shown && (<ViewBoundary key={view} name={currentNav?.label ?? "This section"}>
            {view === "overview" && (
              <OverviewView
                events={events}
                seed={seed}
                wsConnected={wsConnected}
                navigate={navigate}
                onAfterNavigate={() => setNavOpen(false)}
              />
            )}
            {view === "labs" && <LabsView />}
            {view === "games" && (
              <ArenaView
                initialSessionId={selectedSessionId}
                initialStep={selectedStep}
                lens={lens}
                refreshTrigger={sessionsRefresh}
                onSessionShown={onSessionShown}
              />
            )}
            {view === "deals" && (
              <GamesView
                section={view}
                initialSessionId={selectedSessionId}
                initialGameModule={gameModuleType}
                refreshTrigger={sessionsRefresh}
                onSessionShown={onSessionShown}
              />
            )}
            {view === "rooms" && <RoomsView initialRoomId={selectedRoomId} navigate={navigate} />}
            {view === "agents" && (
              <AgentsView initialAgentId={selectedAgentId} navigate={navigate} />
            )}
            {view === "feed" && <FeedView />}
            {view === "agora" && <AgorasView navigate={navigate} />}
            {view === "messages" && (
              <MessagesView
                navigate={navigate}
                liveEvent={agentWs.last}
                liveConnected={agentWs.connected}
              />
            )}
            {view === "fees" && !isSpectator && <FeesView navigate={navigate} isOperator={isRelayOperator} />}
            {view === "stats" && !isSpectator && <StatsView />}
            {view === "profile" && <ProfileView navigate={navigate} />}
            {view === "docs" && <DocsView />}
            {view === "settings" && <SettingsView />}
            {view === "relay" && isRelayOperator && <RelayView />}
            {view === "operator" && isRelayOperator && <OperatorView />}
            {view === "season" && (
              <>
                <GamifyView initialAgentId={selectedAgentId} />
                <div className="e-view-body">
                  <details className="e-badge-fold">
                    <summary>All badges and how each is earned</summary>
                    <BadgeGallery embedded />
                  </details>
                </div>
              </>
            )}
            </ViewBoundary>)}
            <AnimaticHudFoot section={currentNav?.label.toLowerCase() ?? view} />
          </div>
          <BottomBar view={view} onGoDest={goToDest} badges={inboxUnread > 0 ? { messages: inboxUnread } : undefined} />
          </div>
        </main>
        {/* Full-page chat (a phone, a narrow tab) keeps the menu: its ☰ opens the same drawer, above it. */}
        <AgentChatDrawer onMenu={() => setNavOpen(true)} covered={navOpen} />
    </div>
    </RelayHealthCtx.Provider>
  );
}