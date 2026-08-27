import React, { useEffect, useState } from "react";
import { api } from "./api";
import type { UiConfig } from "./api";
import { RELAY, VIEWER_ID } from "./relayEnv";
import { RelayCtx } from "./relayCtx";
import type { RelayCtxValue } from "./relayCtx";

/** sessionStorage key for the dashboard JWT — only the provider touches it. */
const SESSION_TOKEN_KEY = "emporia_dashboard_token";

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
    requireChallenge: cfg?.require_challenge ?? false,
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

