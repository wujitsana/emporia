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
import { RelayContextProvider, useRelayCtx } from "./relayContext";
import { applyTheme, THEMES, TINTS, type Theme, type Tint } from "./themeConfig";
import { useGlobalEvents, useInterval } from "./hooks";
import { NAV, type View } from "./navConfig";
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
  FeesView,
} from "./views";
import { VIEWER } from "./relayEnv";
import { syncHash, viewFromHash } from "./dashboardRoute";

const LS_THEME = "emporia_theme";
const LS_TINT = "emporia_tint";

function readTheme(): Theme {
  const v = localStorage.getItem(LS_THEME);
  return (THEMES as readonly string[]).includes(v ?? "") ? (v as Theme) : "theme-dark";
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
  const [gameModuleType, setGameModuleType] = useState<string | null>(null);
  const [listingPeek, setListingPeek] = useState<NavOpts["listingPeek"] | null>(null);

  const [sessionsRefresh, setSessionsRefresh] = useState(0);
  const [listingsRefresh, setListingsRefresh] = useState(0);
  const [eventsRefresh, setEventsRefresh] = useState(0);
  const [inboxUnread, setInboxUnread] = useState(0);

  const { events, seed, wsConnected } = useGlobalEvents();
  const [healthPoll] = useInterval(() => api.health(), 5_000);
  useEffect(() => {
    if (!VIEWER) return;
    const poll = () => api.agentInbox(VIEWER, true, 1).then((d) => setInboxUnread(d.count)).catch(() => {});
    poll();
    const t = setInterval(poll, 15_000);
    return () => clearInterval(t);
  }, []);
  const relayHealth = healthPoll ?? seed;

  useEffect(() => {
    const ev = events[0];
    if (!ev) return;
    if (ev.type === "session_created" || ev.type === "session_completed")
      setSessionsRefresh((n) => n + 1);
    if (ev.type === "listing_created") setListingsRefresh((n) => n + 1);
    if (ev.type === "event_created") setEventsRefresh((n) => n + 1);
  }, [events[0]?._ts]);

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
      if (opts?.gameModuleType !== undefined) setGameModuleType(opts.gameModuleType || null);
      if (opts?.listingPeek !== undefined) setListingPeek(opts.listingPeek ?? null);
    },
    [applyView],
  );

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      const found = NAV.find((n) => n.hotkey === e.key);
      if (found) goToView(found.id);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [goToView]);

  return (
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
              />
            )}
            {view === "rooms" && <RoomsView initialRoomId={selectedRoomId} navigate={navigate} />}
            {view === "events" && <EventsView refreshTrigger={eventsRefresh} />}
            {view === "agents" && (
              <AgentsView initialAgentId={selectedAgentId} listingPeek={listingPeek} navigate={navigate} />
            )}
            {view === "agoras" && <AgorasView navigate={navigate} />}
            {view === "messages" && <MessagesView navigate={navigate} />}
            {view === "fees" && !isSpectator && <FeesView navigate={navigate} isOperator={isRelayOperator} />}
            {view === "profile" && <ProfileView />}
          </div>
        </main>
      </div>
  );
}