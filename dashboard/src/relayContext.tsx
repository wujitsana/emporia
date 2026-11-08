import React, { useEffect, useState } from "react";
import { api } from "./api";
import type { UiConfig } from "./api";
import { RELAY, VIEWER_ID, SESSION_TOKEN_KEY } from "./relayEnv";
import { RelayCtx } from "./relayCtx";
import type { RelayCtxValue } from "./relayCtx";


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
  // Operator section: the relay's public config names its operator, and the viewer's id is
  // only what this page was opened as — a claim. The operator's sections open once the
  // relay itself says so from the viewer's signed sign-in (GET /operator/whoami).
  const claimsOperator = !!(viewerId && relayOwner && viewerId === relayOwner);
  const [verifiedOperator, setVerifiedOperator] = useState<string | null>(null);
  useEffect(() => {
    if (!claimsOperator) return;      // no claim: not the operator, whatever was verified before
    let live = true;
    api.operatorWhoami()
      .then((w) => { if (live) setVerifiedOperator(w.is_operator ? w.agent_id : null); })
      // A refusal (401/403) is an answer; a relay that did not answer keeps the last one.
      .catch((e) => { if (live && /^40[13]\b/.test(String(e?.message ?? ""))) setVerifiedOperator(null); });
    return () => { live = false; };
  }, [claimsOperator, viewerId, tick, dashboardToken]);
  const isRelayOperator = claimsOperator && verifiedOperator === viewerId;
  const relayUrl = cfg?.relay_url ?? RELAY;

  const value: RelayCtxValue = {
    viewerId,
    relayOwner,
    relayId: cfg?.relay_id ?? null,
    relayUrl,
    requireChallenge: cfg?.require_challenge ?? false,
    agentCount: cfg?.agent_count ?? 0,
    activeSessionCount: cfg?.active_session_count ?? 0,
    relayVersion: cfg?.version ?? "",
    isRelayOperator,
    isSpectator: viewerId === null,
    dashboardToken,
    setDashboardToken,
    refresh: () => setTick((n) => n + 1),
  };

  return <RelayCtx.Provider value={value}>{children}</RelayCtx.Provider>;
}

