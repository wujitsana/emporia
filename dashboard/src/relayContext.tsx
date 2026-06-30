import React, { createContext, useContext, useEffect, useState } from "react";
import { api } from "./api";
import type { UiConfig } from "./api";
import { RELAY } from "./relayEnv";

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
  /** True when the relay is on localhost — identity header trusted without JWT */
  isLocalRelay: boolean;
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
  isSpectator: true,
  isLocalRelay: true,
  dashboardToken: null,
  setDashboardToken: () => {},
  refresh: () => {},
};

const SESSION_TOKEN_KEY = "emporia_dashboard_token";

const RelayCtx = createContext<RelayCtxValue>(DEFAULT);

const VITE_AGENT_ID = (import.meta.env.VITE_AGENT_ID as string | undefined) ?? null;

function _isLocalUrl(url: string): boolean {
  try {
    const h = new URL(url).hostname;
    return h === "localhost" || h === "127.0.0.1" || h === "::1";
  } catch {
    return true;
  }
}

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

  const viewerId = VITE_AGENT_ID;
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
    isSpectator: viewerId === null,
    isLocalRelay: _isLocalUrl(relayUrl),
    dashboardToken,
    setDashboardToken,
    refresh: () => setTick((n) => n + 1),
  };

  return <RelayCtx.Provider value={value}>{children}</RelayCtx.Provider>;
}

export function useRelayCtx(): RelayCtxValue {
  return useContext(RelayCtx);
}
