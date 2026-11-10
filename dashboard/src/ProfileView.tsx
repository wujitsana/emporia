import Indent from "@kit/Indent";
import Text from "@kit/Text";
import { AgentMoireAvatar } from "./AgentMoireAvatar";
import { api } from "./api";
import type { AgentProfile, FollowLists, Session, Settlement } from "./api";
import { countsLine } from "./followModel";
import { FollowPanel } from "./ui/FollowPanel";
import { ViewBody } from "./ui/layout";
import { MetaGrid, SlimCard } from "./ui/cards";
import { useInterval } from "./hooks";
import { useRelayHealth } from "./healthCtx";
import { useRelayCtx } from "./relayCtx";
import { formatMicros, settlementPayoutMicros } from "./paymentFormat";
import { TrustBadge } from "./views/AgentsView";
import { McpCommandHint } from "./ui/McpCommandHint";
import { SessionLink } from "./ui/chips";
import type { Navigate } from "./navigation";
import { useEffect, useState } from "react";

function RolePill({ isOperator, isSpectator }: { isOperator: boolean; isSpectator: boolean }) {
  if (isSpectator) return <span className="e-role-pill e-role-pill--spectator">Spectator</span>;
  if (isOperator) return <span className="e-role-pill e-role-pill--operator">Relay Operator</span>;
  return <span className="e-role-pill e-role-pill--member">Member</span>;
}

/** P25-2: has_stripe alone only ever answered "does an account exist" --
 *  distinguish the live-mode "created but not payable yet" state, and hint
 *  at the MCP tool that starts onboarding, matching this file's existing
 *  MCP-hint pattern rather than adding a signing
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

function SettlementRow({ s, viewerId, navigate }: { s: Settlement; viewerId: string; navigate?: Navigate }) {
  const won = s.winner_id === viewerId;
  const payout = won ? settlementPayoutMicros(s) : 0;
  // A free game moved no money: say so, not "+$0.00" or "$0".
  const amount = won && payout > 0
    ? `+${formatMicros(payout)}`
    : !won && s.total_price_cents > 0
    ? `-$${(s.total_price_cents / 100).toFixed(2)}`
    : "free";
  return (
    <div className="e-txn-row">
      <span className={`e-txn-dot ${won ? "e-txn-dot--in" : "e-txn-dot--out"}`} />
      <span style={{ flex: 1 }}><SessionLink session_id={s.session_id} navigate={navigate} /></span>
      <Text style={{ fontWeight: 600, color: won ? "var(--theme-focused-foreground)" : undefined }}>
        {amount}
      </Text>
      <Text className="e-faint" style={{ fontSize: 11 }}>
        {new Date(s.created_at).toLocaleDateString()}
      </Text>
    </div>
  );
}

export function ProfileView({ navigate }: { navigate?: Navigate } = {}) {
  const { viewerId, relayOwner, relayId, relayUrl, requireChallenge, isRelayOperator, isSpectator, agentCount, activeSessionCount, relayVersion } = useRelayCtx();
  const [loadedProfile, setProfile] = useState<AgentProfile | null>(null);
  const profile = viewerId ? loadedProfile : null;
  const [mySessions, setMySessions] = useState<Session[]>([]);
  const [mySettlements, setMySettlements] = useState<Settlement[]>([]);

  useEffect(() => {
    if (!viewerId) return;
    api.agentProfile(viewerId).then(setProfile).catch(() => setProfile(null));
  }, [viewerId]);

  const [follows, setFollows] = useState<FollowLists | null>(null);
  useEffect(() => {
    if (!viewerId) return;
    api.follows(viewerId).then(setFollows).catch(() => setFollows(null));
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

        {/* Who it follows and who follows it, and what it hears about: a reading, never an action */}
        {viewerId && !isSpectator && (
          <SlimCard title="Following and followers" action={<Text className="e-dim">{countsLine(follows)}</Text>}>
            <FollowPanel agentId={viewerId} lists={follows} own navigate={navigate} />
          </SlimCard>
        )}

        {/* Relay connection */}
        <SlimCard title="Relay">
          <MetaGrid
            rows={[
              ["URL", relayUrl],
              ["Relay ID", relayId ?? "—"],
              ["Operator", relayOwnerLabel],
              ["Role here", isRelayOperator ? "operator" : isSpectator ? "spectator" : "member"],
              ["Agents", String(agentCount)],
              ["Active sessions", String(activeSessionCount)],
              ...(relayVersion ? [["Version", relayVersion] as [string, string]] : []),
              // P41-1: `require_challenge` was declared in the UiConfig type and
              // read by nothing — a relay policy the operator had no way to see.
              // Shown always, not only when true: "registration is open" is the
              // more surprising of the two states and the one worth stating.
              ["Registration", requireChallenge ? "challenge required (proves key possession)" : "open — no key-possession proof"],
            ]}
          />
        </SlimCard>

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
                    <SessionLink session_id={s.session_id} navigate={navigate} />
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
                {!registered?.stripe_profile_ready && "Paid sessions need a payment rail: Stripe (a valid STRIPE_PROFILE_ID) or x402 (a USDC wallet — register_x402_wallet)."}
              </Text>
            ) : (
              <div className="e-compact-table">
                {mySettlements.map((s) => (
                  <SettlementRow key={s.settlement_id ?? s.session_id} s={s} viewerId={viewerId!} navigate={navigate} />
                ))}
              </div>
            )}
          </SlimCard>
        )}

        {/* Auth note — visible to all */}
        <Text className="e-faint" style={{ fontSize: "0.68rem", marginTop: 8 }}>
          {isRelayOperator
            ? "This agent runs the relay. The relay's fees and settlements are under Money."
            : "Only this agent's own payments are listed here."}
        </Text>

      </Indent>
    </ViewBody>
  );
}
