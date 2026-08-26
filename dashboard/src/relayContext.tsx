import React, { createContext, useContext, useEffect, useState } from "react";
import { api } from "./api";
import type { UiConfig } from "./api";
import { RELAY, VIEWER_ID } from "./relayEnv";

/**
 * viewerId  — who is using THIS dashboard (VITE_AGENT_ID, set per profile at install time)
 * relayOwner — who operates the relay (from /ui-config; null = bare server, no agent operator)
 *
 * In a federated network, these are independent:
 *   - You (hackathon_hermes) connect to a relay run by someone else → viewerId ≠ relayOwner
 *   - You run your own relay → viewerId === relayOwner → isRelayOperator = true
 *
 * Auth note: dashboard sessions are scoped to localhost for now.
 * Production path: POST /dashboard/challenge → sign with agent Ed25519 key via MCP
 *   → POST /dashboard/session → relay issues short-lived JWT → Bearer token on requests.
 */
export interface RelayCtxValue {
  viewerId: string | null;
  relayOwner: string | null;
  relayId: string | null;
  relayUrl: string;
  requireNous: boolean;
  writeRequiresNous: boolean;
  agentCount: number;
  activeSessionCount: number;
  relayVersion: string;
  isRelayOperator: boolean;
  isSpectator: boolean;
  /** P36-5: the relay's own answer to "will you accept my X-Emporia-Agent-Id
   *  header?", per connection.
   *
   *  This replaced `isLocalRelay`, which was standing in for it and getting it
   *  wrong — header auth is off unless the operator sets
   *  EMPORIA_TRUST_LOCAL_HEADER_AUTH=1, so "the relay is on localhost" says
   *  nothing about whether it will authenticate you. `isLocalRelay` had no
   *  other reader and defaulted to `true`, i.e. it failed open; it is gone
   *  rather than left around for the next person to reach for. */
  headerAuthTrusted: boolean;
  /** Bearer JWT for remote relay auth (sessionStorage-backed) */
  dashboardToken: string | null;
  setDashboardToken: (t: string | null) => void;
  refresh: () => void;
}

const DEFAULT: RelayCtxValue = {
  viewerId: null,
  relayOwner: null,
  relayId: null,
  relayUrl: RELAY,
  requireNous: false,
  writeRequiresNous: false,
  agentCount: 0,
  activeSessionCount: 0,
  relayVersion: "",
  isRelayOperator: false,
  headerAuthTrusted: false,
  isSpectator: true,
  dashboardToken: null,
  setDashboardToken: () => {},
  refresh: () => {},
};

const SESSION_TOKEN_KEY = "emporia_dashboard_token";

const RelayCtx = createContext<RelayCtxValue>(DEFAULT);

// P35-1: viewer identity now comes from relayEnv.VIEWER_ID, not a second
// private read of the same env var with its own idea of what "unset" means.


export function RelayContextProvider({ children }: { children: React.ReactNode }) {
  const [cfg, setCfg] = useState<UiConfig | null>(null);
  const [tick, setTick] = useState(0);
  const [dashboardToken, setDashboardTokenState] = useState<string | null>(
    () => sessionStorage.getItem(SESSION_TOKEN_KEY)
  );

  useEffect(() => {
    api.uiConfig()
      .then(setCfg)
      .catch(() => {});
  }, [tick]);

  // Refresh relay metadata every 30s
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 30_000);
    return () => clearInterval(id);
  }, []);

  const setDashboardToken = (t: string | null) => {
    setDashboardTokenState(t);
    if (t) sessionStorage.setItem(SESSION_TOKEN_KEY, t);
    else sessionStorage.removeItem(SESSION_TOKEN_KEY);
  };

  const viewerId = VIEWER_ID;
  const relayOwner = cfg?.owner_agent_id ?? null;
  const isRelayOperator = !!(viewerId && relayOwner && viewerId === relayOwner);
  const relayUrl = cfg?.relay_url ?? RELAY;

  const value: RelayCtxValue = {
    viewerId,
    relayOwner,
    relayId: cfg?.relay_id ?? null,
    relayUrl,
    requireNous: cfg?.require_nous ?? false,
    writeRequiresNous: cfg?.write_requires_nous ?? false,
    agentCount: cfg?.agent_count ?? 0,
    activeSessionCount: cfg?.active_session_count ?? 0,
    relayVersion: cfg?.version ?? "",
    isRelayOperator,
    headerAuthTrusted: cfg?.header_auth_trusted ?? false,
    isSpectator: viewerId === null,
    dashboardToken,
    setDashboardToken,
    refresh: () => setTick((n) => n + 1),
  };

  return <RelayCtx.Provider value={value}>{children}</RelayCtx.Provider>;
}

export function useRelayCtx(): RelayCtxValue {
  return useContext(RelayCtx);
}
