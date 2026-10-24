import Indent from "@components/Indent";
import Text from "@components/Text";
import { AgentMoireAvatar } from "./AgentMoireAvatar";
import { api, dashboardAuth } from "./api";
import type { AgentProfile, Session, Settlement } from "./api";
import { ViewBody } from "./ui/layout";
import { MetaGrid, SlimCard } from "./ui/cards";
import { useInterval } from "./hooks";
import { useRelayHealth } from "./healthCtx";
import { useRelayCtx } from "./relayCtx";
import { formatMicros, settlementPayoutMicros } from "./paymentFormat";
import { TrustBadge } from "./views/AgentsView";
import { McpCommandHint } from "./ui/McpCommandHint";
import { useEffect, useRef, useState } from "react";

/**
 * Auto-starts the dashboard auth flow when the relay will not accept a header
 * identity and no JWT is stored. No button — polls silently until the agent
 * completes the sign_dashboard_challenge MCP call.
 *
 * P36-5: this used to gate on `isLocalRelay`, i.e. "is the relay on my machine",
 * on the assumption that a co-located relay trusts `X-Emporia-Agent-Id`. It does
 * not unless the operator sets EMPORIA_TRUST_LOCAL_HEADER_AUTH=1, which is off
 * by default — so the standard local setup hid this panel AND had every
 * agent-scoped view render a bare `401 /agents` with no way to act on it. The
 * relay knows the answer per connection; ask it.
 */
function RemoteAuthPanel() {
  const { viewerId, relayUrl, headerAuthTrusted, dashboardToken, setDashboardToken } = useRelayCtx();
  const [state, setState] = useState<"idle" | "pending" | "done" | "error">("idle");
  const [challenge, setChallenge] = useState<{ challenge_id: string; nonce: string } | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Auto-start challenge when remote relay + no JWT + we have an identity
  useEffect(() => {
    if (headerAuthTrusted || dashboardToken || !viewerId || state !== "idle") return;
    setState("pending");
    dashboardAuth.challenge()
      .then((c) => {
        setChallenge({ challenge_id: c.challenge_id, nonce: c.nonce });
        // Poll relay until agent completes the sign
        pollRef.current = setInterval(async () => {
          try {
            const res = await dashboardAuth.poll(c.challenge_id);
            if (res.ready && res.token) {
              setDashboardToken(res.token);
              setState("done");
              if (pollRef.current) clearInterval(pollRef.current);
            }
          } catch {}
        }, 2000);
      })
      .catch((e: unknown) => {
        setErrMsg(String(e));
        setState("error");
      });
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [headerAuthTrusted, dashboardToken, viewerId, state, setDashboardToken]);

  if (headerAuthTrusted || dashboardToken) return null;
  if (!viewerId) return null;

  return (
    <SlimCard title="Relay sign-in">
      {state === "pending" && challenge ? (
        <div>
          <Text className="e-dim" style={{ marginBottom: 8 }}>
            Run this MCP tool to authenticate, then this panel will auto-complete:
          </Text>
          <code style={{ display: "block", fontSize: 11, padding: "8px", background: "rgba(128,128,128,0.1)", borderRadius: 4, wordBreak: "break-all" }}>
            {`sign_dashboard_challenge(relay_url="${relayUrl}", challenge_id="${challenge.challenge_id}", nonce="${challenge.nonce}")`}
          </code>
          <Text className="e-faint" style={{ marginTop: 8, fontSize: 11 }}>Waiting for signature… polling every 2s</Text>
        </div>
      ) : state === "done" ? (
        <Text style={{ color: "var(--theme-focused-foreground)" }}>✓ Authenticated — JWT stored for this session</Text>
      ) : state === "error" ? (
        <Text className="e-faint">Auth failed: {errMsg}</Text>
      ) : null}
    </SlimCard>
  );
}

function RolePill({ isOperator, isSpectator }: { isOperator: boolean; isSpectator: boolean }) {
  if (isSpectator) return <span className="e-role-pill e-role-pill--spectator">Spectator</span>;
  if (isOperator) return <span className="e-role-pill e-role-pill--operator">Relay Operator</span>;
  return <span className="e-role-pill e-role-pill--member">Member</span>;
}

/** P25-2: has_stripe alone only ever answered "does an account exist" --
 *  distinguish the live-mode "created but not payable yet" state, and hint
 *  at the MCP tool that starts onboarding, matching this file's existing
 *  MCP-hint pattern (RemoteAuthPanel above) rather than adding a signing
 *  write-flow to the dashboard itself. */
function stripeAccountStatusLabel(registered: AgentProfile, stripeMode: string): string {
  const status = registered.stripe_account_status ?? (registered.has_stripe ? "active" : "none");
  if (status === "active") return "connected";
  if (status === "pending") return "onboarding pending — Stripe confirms automatically";
  // P37-2: the advice is opposite in the two key spaces, and this used to give
  // the live-mode one unconditionally. In test mode the relay auto-provisions a
  // Connected Account at registration and POST /agents/{id}/stripe-onboarding
  // *rejects* the call — so "run start_stripe_onboarding" was an instruction
  // that could only produce a 400.
  if (stripeMode === "test") return "not connected — test mode provisions an account at registration";
  if (stripeMode !== "live") return "not connected — this relay has no Stripe key configured";
  return "not connected — needs Stripe onboarding for live payouts";
}

function SettlementRow({ s, viewerId }: { s: Settlement; viewerId: string }) {
  const won = s.winner_id === viewerId;
  const amount = won
    ? `+${formatMicros(settlementPayoutMicros(s))}`
    : s.total_stake_cents > 0
    ? `-$${(s.total_stake_cents / 100).toFixed(2)}`
    : "$0";
  return (
    <div className="e-txn-row">
      <span className={`e-txn-dot ${won ? "e-txn-dot--in" : "e-txn-dot--out"}`} />
      <Text className="e-dim" style={{ fontFamily: "var(--font-family-mono)", fontSize: 11, flex: 1 }}>
        …{s.session_id.slice(-10)}
      </Text>
      <Text style={{ fontWeight: 600, color: won ? "var(--theme-focused-foreground)" : undefined }}>
        {amount}
      </Text>
      <Text className="e-faint" style={{ fontSize: 11 }}>
        {new Date(s.created_at).toLocaleDateString()}
      </Text>
    </div>
  );
}

export function ProfileView() {
  const { viewerId, relayOwner, relayId, relayUrl, requireNous, writeRequiresNous, requireChallenge, isRelayOperator, isSpectator, agentCount, activeSessionCount, relayVersion } = useRelayCtx();
  const [profile, setProfile] = useState<AgentProfile | null>(null);
  const [mySessions, setMySessions] = useState<Session[]>([]);
  const [mySettlements, setMySettlements] = useState<Settlement[]>([]);

  useEffect(() => {
    if (!viewerId) { setProfile(null); return; }
    api.agentProfile(viewerId).then(setProfile).catch(() => setProfile(null));
  }, [viewerId]);

  useEffect(() => {
    if (!viewerId) return;
    api.agentSessions(viewerId).then((d) => setMySessions(d.sessions.slice(0, 5))).catch(() => {});
    api.agentSettlements(viewerId).then((d) => setMySettlements(d.settlements.slice(0, 8))).catch(() => {});
  }, [viewerId]);

  const [agentsData] = useInterval(() => api.agents(), 30_000);
  // P43-2: was a fourth independent /health poll; App polls it once.
  const health = useRelayHealth();
  const stripeMode = health?.stripe_mode ?? "";
  const agents = agentsData?.agents ?? [];
  const registered = profile ?? agents.find((a) => a.agent_id === viewerId) ?? null;
  const displayName = registered?.display_name || registered?.agent_id || viewerId || "Spectator";

  const trustCell = registered ? <TrustBadge a={registered} /> : null;
  const relayOwnerLabel = relayOwner ?? "standalone server";
  const totalEarned = mySettlements
    .filter((s) => s.winner_id === viewerId)
    .reduce((n, s) => n + settlementPayoutMicros(s), 0);

  return (
    <ViewBody>
      <Indent>

        {/* Identity card */}
        <SlimCard title="Connected agent">
          <div className="e-profile-agent">
            <AgentMoireAvatar agentId={displayName} size={48} />
            <div style={{ flex: 1 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <Text style={{ fontSize: 16, fontWeight: 600 }}>{displayName}</Text>
                <RolePill isOperator={isRelayOperator} isSpectator={isSpectator} />
              </div>
              {registered?.agent_id && registered.agent_id !== displayName && (
                <Text className="e-dim" style={{ fontSize: 11, fontFamily: "var(--font-family-mono)" }}>
                  {registered.agent_id}
                </Text>
              )}
            </div>
          </div>

          {registered ? (
            <MetaGrid
              rows={[
                // P42: extracted rather than written inline. `jsx-key` fires on
                // any JSX element sitting directly in an array literal, and it
                // cannot see that MetaGrid renders each row as
                // `<div key={k}><dd>{v}</dd></div>` — the wrapper carries the
                // key, so a key here would be ignored. P34 verified that by
                // reading cards.tsx and left the warning rather than silence it
                // with a disable comment every future reader would re-justify.
                // A named value satisfies the rule without asserting anything
                // false about where the key lives.
                ["Trust", trustCell],
                ["Registered", new Date(registered.registered_at).toLocaleDateString()],
                ["Sessions", String(registered.session_count)],
                ["Wins", `${registered.win_count} (${registered.session_count > 0 ? Math.round((registered.win_count / registered.session_count) * 100) : 0}%)`],
                ["Stripe payouts", stripeAccountStatusLabel(registered, stripeMode)],
                ["Stripe MPP seller", registered.stripe_profile_ready ? "ready" : "not ready"],
                ...(registered.payment_methods?.length ? [["Methods", registered.payment_methods.join(", ")] as [string, string]] : []),
                ...(registered.payment_rails?.length ? [["Rails", registered.payment_rails.join(", ")] as [string, string]] : []),
              ]}
            />
          ) : !isSpectator ? (
            <Text className="e-faint" style={{ marginTop: 10 }}>
              Not yet registered on this relay. Use the <code>register_agent</code> MCP tool to join.
            </Text>
          ) : (
            <Text className="e-faint" style={{ marginTop: 10 }}>
              Spectator mode — set <code>VITE_AGENT_ID</code> in dashboard <code>.env.local</code> to identify as an agent.
            </Text>
          )}
        </SlimCard>

        {/* Relay connection */}
        <SlimCard title="Relay">
          <MetaGrid
            rows={[
              ["URL", relayUrl],
              ["Relay ID", relayId ?? "—"],
              ["Operator", relayOwnerLabel],
              ["Your role", isRelayOperator ? "operator" : isSpectator ? "spectator" : "member"],
              ["Agents", String(agentCount)],
              ["Active sessions", String(activeSessionCount)],
              ...(relayVersion ? [["Version", relayVersion] as [string, string]] : []),
              ...(requireNous ? [["Nous-gated", "registration requires Nous JWT"] as [string, string]] : []),
              ...(writeRequiresNous ? [["Write-gated", "writes require Nous verification"] as [string, string]] : []),
              // P41-1: `require_challenge` was declared in the UiConfig type and
              // read by nothing — a relay policy the operator had no way to see.
              // Shown always, not only when true: "registration is open" is the
              // more surprising of the two states and the one worth stating.
              ["Registration", requireChallenge ? "challenge required (proves key possession)" : "open — no key-possession proof"],
            ]}
          />
        </SlimCard>

        {/* Remote relay auth — auto-starts when needed */}
        <RemoteAuthPanel />

        {/* My recent sessions */}
        {!isSpectator && (
          <SlimCard
            title="Recent sessions"
            action={<Text className="e-dim">{mySessions.length}</Text>}
          >
            {mySessions.length === 0 ? (
              <Text className="e-faint">No sessions yet. Create one via MCP or Listings.</Text>
            ) : (
              <div className="e-compact-table">
                {mySessions.map((s) => (
                  <div key={s.session_id} className="e-txn-row">
                    <span className={`e-session-status-dot e-session-status-dot--${s.status}`} />
                    <Text className="e-dim" style={{ fontFamily: "var(--font-family-mono)", fontSize: 11, flex: 1 }}>
                      {s.module_type.replace("emporia:", "").replace(/:v\d$/, "")}
                    </Text>
                    <Text className="e-faint" style={{ fontSize: 11 }}>{s.status}</Text>
                    <Text className="e-faint" style={{ fontSize: 11 }}>
                      {new Date(s.created_at).toLocaleDateString()}
                    </Text>
                  </div>
                ))}
              </div>
            )}
          </SlimCard>
        )}

        {/* P37-2: live-mode payout onboarding. Only shown when it can actually
            do something: a live key, an identified viewer, and an account that
            is not already active. `start_stripe_onboarding` returns a URL the
            operator opens in a browser to walk Stripe's hosted KYC — the
            dashboard cannot perform it, which is what the hint is for. */}
        {!isSpectator && viewerId && stripeMode === "live"
          && (registered?.stripe_account_status ?? (registered?.has_stripe ? "active" : "none")) !== "active" && (
          <SlimCard title="Payouts">
            <Text className="e-faint" style={{ display: "block", marginBottom: 8 }}>
              {(registered?.stripe_account_status ?? "none") === "pending"
                ? "Stripe is still verifying this account. Re-run the tool for a fresh onboarding link if the last one expired — Stripe confirms the result automatically."
                : "Live payouts need a Stripe Connected Account. The tool returns a link to Stripe's own verification flow; open it in a browser."}
            </Text>
            <McpCommandHint
              label="Start Stripe onboarding via MCP"
              tool="start_stripe_onboarding"
              args={{ agent_id: viewerId, email: "you@example.com", country: "US" }}
            />
            <Text className="e-faint" style={{ fontSize: "0.68rem", marginTop: 8, display: "block" }}>
              Use a real, reachable address — Stripe sends verification requirements and payout
              failures there. Country is fixed once the account exists.
            </Text>
          </SlimCard>
        )}

        {/* My settlement history */}
        {!isSpectator && (
          <SlimCard
            title="Transaction history"
            action={
              totalEarned > 0 ? (
                <Text style={{ color: "var(--theme-focused-foreground)", fontSize: 12 }}>
                  +{formatMicros(totalEarned)} earned
                </Text>
              ) : undefined
            }
          >
            {mySettlements.length === 0 ? (
              <Text className="e-faint">
                No settled transactions yet.{" "}
                {!registered?.stripe_profile_ready && !(registered?.payment_methods || []).includes("tempo") && "Paid sessions need a payment rail: Stripe (a valid STRIPE_PROFILE_ID, or Tempo) or x402 (a USDC wallet — register_x402_wallet)."}
              </Text>
            ) : (
              <div className="e-compact-table">
                {mySettlements.map((s) => (
                  <SettlementRow key={s.settlement_id ?? s.session_id} s={s} viewerId={viewerId!} />
                ))}
              </div>
            )}
          </SlimCard>
        )}

        {/* Auth note — visible to all */}
        <Text className="e-faint" style={{ fontSize: "0.68rem", marginTop: 8 }}>
          {isRelayOperator
            ? "You are the relay operator. Fees & settlement ledger visible in the Fees tab."
            : "Transaction data is scoped to your agent. Production auth: Ed25519 challenge via MCP → relay issues dashboard JWT."}
        </Text>

      </Indent>
    </ViewBody>
  );
}
