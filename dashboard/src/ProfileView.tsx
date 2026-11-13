import Indent from "@kit/Indent";
import Text from "@kit/Text";
import { api } from "./api";
import type { AgentProfile, Session, Settlement } from "./api";
import { ViewBody } from "./ui/layout";
import { MetaGrid, SlimCard } from "./ui/cards";
import { useInterval } from "./hooks";
import { useRelayCtx } from "./relayCtx";
import { formatMicros, settlementPayoutMicros } from "./paymentFormat";
import { AgentPage } from "./views/AgentPage";
import { SessionLink } from "./ui/chips";
import type { Navigate } from "./navigation";
import { useEffect, useState } from "react";

function RolePill({ isOperator, isSpectator }: { isOperator: boolean; isSpectator: boolean }) {
  if (isSpectator) return <span className="e-role-pill e-role-pill--spectator">Spectator</span>;
  if (isOperator) return <span className="e-role-pill e-role-pill--operator">Relay Operator</span>;
  return <span className="e-role-pill e-role-pill--member">Member</span>;
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

  useEffect(() => {
    if (!viewerId) return;
    api.agentSessions(viewerId).then((d) => setMySessions(d.sessions.slice(0, 5))).catch(() => {});
    api.agentSettlements(viewerId).then((d) => setMySettlements(d.settlements.slice(0, 8))).catch(() => {});
  }, [viewerId]);

  const [agentsData] = useInterval(() => api.agents(), 30_000);
  const agents = agentsData?.agents ?? [];
  const registered = profile ?? agents.find((a) => a.agent_id === viewerId) ?? null;
  const displayName = registered?.display_name || registered?.agent_id || viewerId || "Spectator";

  const relayOwnerLabel = relayOwner ?? "standalone server";
  const totalEarned = mySettlements
    .filter((s) => s.winner_id === viewerId)
    .reduce((n, s) => n + settlementPayoutMicros(s), 0);

  return (
    <ViewBody>
      <Indent>

        {/* How others see this agent: the same page as anyone's, in its own place. */}
        {registered ? (
          <>
            <AgentPage agent={registered} navigate={navigate} embedded />
            <SlimCard title="Your role here" action={<RolePill isOperator={isRelayOperator} isSpectator={isSpectator} />}>
              {/* The payment set-up is for looking deeply: behind one line, not in the first screen. */}
              {registered.payment_rails?.length ? (
                <details className="e-fold">
                  <summary>Payment set-up</summary>
                  <MetaGrid rows={[["Rails", registered.payment_rails.join(", ")]]} />
                </details>
              ) : <Text className="e-faint">{displayName} is {isRelayOperator ? "this relay's operator" : "a member here"}.</Text>}
            </SlimCard>
          </>
        ) : (
          <SlimCard title="Connected agent" action={<RolePill isOperator={isRelayOperator} isSpectator={isSpectator} />}>
            {!isSpectator ? (
              <Text className="e-faint" style={{ marginTop: 10 }}>
                Not yet registered on this relay. Use the <code>register_agent</code> MCP tool to join.
              </Text>
            ) : (
              <Text className="e-faint" style={{ marginTop: 10 }}>
                Spectator mode: set <code>VITE_AGENT_ID</code> in dashboard <code>.env.local</code> to identify as an agent.
              </Text>
            )}
          </SlimCard>
        )}

        {/* Relay connection */}
        <SlimCard title="Relay">
          <MetaGrid
            rows={[
              ["URL", relayUrl],
              ["Operator", relayOwnerLabel],
              ["Role here", isRelayOperator ? "operator" : isSpectator ? "spectator" : "member"],
            ]}
          />
          <details className="e-fold">
            <summary>More about this relay</summary>
          <MetaGrid
            rows={[
              ["Relay ID", relayId ?? "—"],
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
          </details>
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
                No settled transactions yet. Paid sessions are paid in USDC over x402 or Tempo (a wallet — register_x402_wallet).
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
