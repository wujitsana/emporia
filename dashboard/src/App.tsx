import "srcl/global.css";
import "srcl/global-fonts.css";
import "./dashboard.css";

import ActionButton from "@components/ActionButton";
import Providers from "@components/Providers";
import Text from "@components/Text";
import { RelayStrip } from "./RelayStrip";

import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { MobileNavDrawer, SectionTabs } from "./AppNav";
import { ThemeCompact } from "./ThemeControls";
import { ProfileView } from "./ProfileView";
import { RELAY } from "./relayEnv";
import { RelayContextProvider } from "./relayContext";
import { useRelayCtx } from "./relayCtx";
import { ViewerSwitcher } from "./ui/ViewerSwitcher";
import { RelayHealthCtx } from "./healthCtx";
import { applyTheme, THEMES, TINTS, type Theme, type Tint } from "./themeConfig";
import { useAgentWs, useGlobalEvents, useInterval } from "./hooks";
import { navForHotkey, type View } from "./navConfig";
import type { NavOpts } from "./eventNav";
import type { Navigate } from "./navigation";
import {
  OverviewView,
  ListingsView,
  RoomsView,
  EventsView,
  AgentsView,
  AgorasView,
  MessagesView,
  GamesView,
  FeesView, DocsView, SettingsView,
} from "./views";
import { VIEWER_ID } from "./relayEnv";
import { syncHash, viewFromHash } from "./dashboardRoute";

const LS_THEME = "emporia_theme";
const LS_TINT = "emporia_tint";

function readTheme(): Theme {
  const v = localStorage.getItem(LS_THEME);
  return (THEMES as readonly string[]).includes(v ?? "") ? (v as Theme) : "theme-light";
}
function readTint(): Tint {
  const v = localStorage.getItem(LS_TINT);
  return (TINTS as readonly string[]).includes(v ?? "") ? (v as Tint) : "";
}

export default function App() {
  return (
    <Providers>
      <RelayContextProvider>
        <AppInner />
      </RelayContextProvider>
    </Providers>
  );
}

function AppInner() {
  const { isRelayOperator, isSpectator } = useRelayCtx();
  const [view, setView] = useState<View>(() => viewFromHash());
  const [theme, setTheme] = useState<Theme>(readTheme);
  const [tint, setTint] = useState<Tint>(readTint);
  const [navOpen, setNavOpen] = useState(false);

  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [selectedRoomId, setSelectedRoomId] = useState<string | null>(null);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const [gameModuleType, setGameModuleType] = useState<string | null>(null);
  const [listingPeek, setListingPeek] = useState<NavOpts["listingPeek"] | null>(null);

  const [sessionsRefresh, setSessionsRefresh] = useState(0);
  const [listingsRefresh, setListingsRefresh] = useState(0);
  const [eventsRefresh, setEventsRefresh] = useState(0);
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
    const poll = () => api.agentInbox(viewer, true, 1).then((d) => setInboxUnread(d.count)).catch(() => {});
    poll();
    // The interval is the safety net for a dropped socket now, not the way the
    // badge stays current — the `agentWs.last` dep is. Every event type the
    // relay sends on this channel also lands in the agent's inbox.
    const t = setInterval(poll, 60_000);
    return () => clearInterval(t);
  }, [agentWs.last]);
  const relayHealth = healthPoll ?? seed;

  /* P41-2: keyed on the newest event's identity rather than on
     `events[0]?._ts`. `_ts` is `Date.now()`, so two events arriving in the same
     millisecond produce the same key and the second one silently fails to
     trigger a refresh. useGlobalEvents prepends a fresh object per event, so
     identity changes exactly once per event and never collides — and it is a
     plain variable, which is what the dependency checker needs to see. */
  const latestEvent = events[0] ?? null;
  useEffect(() => {
    if (!latestEvent) return;
    if (latestEvent.type === "session_created" || latestEvent.type === "session_completed")
      setSessionsRefresh((n) => n + 1);
    if (latestEvent.type === "listing_created") setListingsRefresh((n) => n + 1);
    if (latestEvent.type === "event_created") setEventsRefresh((n) => n + 1);
  }, [latestEvent]);

  useEffect(() => {
    applyTheme(theme, tint);
  }, [theme, tint]);

  const setThemeAndApply = (t: Theme) => {
    setTheme(t);
    localStorage.setItem(LS_THEME, t);
  };
  const setTintAndApply = (t: Tint) => {
    setTint(t);
    localStorage.setItem(LS_TINT, t);
  };

  const applyView = useCallback((id: View) => {
    if (id === "games") {
      setView("sessions");
      syncHash("sessions");
      return;
    }
    setView(id);
    syncHash(id);
  }, []);

  useEffect(() => {
    const onHash = () => applyView(viewFromHash());
    window.addEventListener("hashchange", onHash);
    if (!window.location.hash) syncHash(viewFromHash());
    return () => window.removeEventListener("hashchange", onHash);
  }, [applyView]);

  const goToView = useCallback(
    (id: View) => {
      applyView(id);
      setNavOpen(false);
      setListingPeek(null);
      setGameModuleType(null);
    },
    [applyView],
  );

  const navigate = useCallback<Navigate>(
    (toView, opts) => {
      applyView(toView);
      if (opts?.sessionId !== undefined) setSelectedSessionId(opts.sessionId);
      if (opts?.roomId !== undefined) setSelectedRoomId(opts.roomId);
      if (opts?.agentId !== undefined) setSelectedAgentId(opts.agentId);
      if (opts?.eventId !== undefined) setSelectedEventId(opts.eventId);
      if (opts?.gameModuleType !== undefined) setGameModuleType(opts.gameModuleType || null);
      if (opts?.listingPeek !== undefined) setListingPeek(opts.listingPeek ?? null);
    },
    [applyView],
  );

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      // P35-2: `navForHotkey` applies the same visibility rule the rendered
      // nav does. Searching the unfiltered NAV here is what let a spectator
      // reach the two sections the nav hides.
      const found = navForHotkey(e.key, { isSpectator });
      if (found) goToView(found.id);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
    // `isSpectator` belongs here: the handler closes over it, and it stops being
    // constant the moment the dashboard/challenge login flow lands (the token
    // setter is already on the context).
  }, [goToView, isSpectator]);

  return (
    <RelayHealthCtx.Provider value={relayHealth}>
    <div className={`e-app-shell e-dashboard-shell e-app-shell--header-nav${navOpen ? " is-nav-open" : ""}`}>
        <aside className="e-app-drawer" aria-label="Menu">
          <MobileNavDrawer
            view={view}
            onGo={goToView}
            theme={theme}
            tint={tint}
            onTheme={setThemeAndApply}
            onTint={setTintAndApply}
            badges={inboxUnread > 0 ? { messages: inboxUnread } : undefined}
          />
        </aside>
        <button
          type="button"
          className="e-app-backdrop"
          aria-label="Close menu"
          onClick={() => setNavOpen(false)}
        />
        <main className="e-app-main">
          <header className="e-app-top">
            <div className="e-app-top__row">
              <Text className="e-brand-title e-brand-title--header">✶ Emporia</Text>
              <div className="e-app-top__actions">
                <ThemeCompact
                  theme={theme}
                  tint={tint}
                  onTheme={setThemeAndApply}
                  onTint={setTintAndApply}
                />
                <ViewerSwitcher />
                <RelayStrip
                  relayUrl={RELAY}
                  wsConnected={wsConnected}
                  health={relayHealth}
                />
              </div>
            </div>
            <SectionTabs
              view={view}
              onGo={goToView}
              variant="horizontal"
              badges={inboxUnread > 0 ? { messages: inboxUnread } : undefined}
            />
          </header>
          <div className="e-mobile-top">
            <ActionButton hotkey="☰" onClick={() => setNavOpen(true)}>
              menu
            </ActionButton>
            <Text className="e-brand-title">✶ Emporia</Text>
            <ThemeCompact
              theme={theme}
              tint={tint}
              onTheme={setThemeAndApply}
              onTint={setTintAndApply}
            />
            <ViewerSwitcher />
            <RelayStrip
              relayUrl={RELAY}
              wsConnected={wsConnected}
              health={relayHealth}
              compact
            />
          </div>
          <div className="e-view">
            {view === "overview" && (
              <OverviewView
                events={events}
                seed={seed}
                wsConnected={wsConnected}
                navigate={navigate}
                onAfterNavigate={() => setNavOpen(false)}
              />
            )}
            {view === "listings" && (
              <ListingsView refreshTrigger={listingsRefresh} navigate={navigate} />
            )}
            {view === "sessions" && (
              <GamesView
                initialSessionId={selectedSessionId}
                initialGameModule={gameModuleType}
                listingPeek={listingPeek}
                refreshTrigger={sessionsRefresh}
              />
            )}
            {view === "rooms" && <RoomsView initialRoomId={selectedRoomId} navigate={navigate} />}
            {view === "events" && (
              <EventsView refreshTrigger={eventsRefresh} initialEventId={selectedEventId} />
            )}
            {view === "agents" && (
              <AgentsView initialAgentId={selectedAgentId} listingPeek={listingPeek} navigate={navigate} />
            )}
            {view === "agoras" && <AgorasView navigate={navigate} />}
            {view === "messages" && (
              <MessagesView
                navigate={navigate}
                liveEvent={agentWs.last}
                liveConnected={agentWs.connected}
              />
            )}
            {view === "fees" && !isSpectator && <FeesView navigate={navigate} isOperator={isRelayOperator} />}
            {view === "profile" && <ProfileView navigate={navigate} />}
            {view === "docs" && <DocsView />}
            {view === "settings" && <SettingsView />}
          </div>
        </main>
    </div>
    </RelayHealthCtx.Provider>
  );
}