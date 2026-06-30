import Indent from "@components/Indent";
import Text from "@components/Text";
import RowSpaceBetween from "@components/RowSpaceBetween";
import { AgentMoireAvatar } from "./AgentMoireAvatar";
import { api, dashboardAuth } from "./api";
import type { AgentProfile, Session, Settlement } from "./api";
import { ViewBody } from "./ui/layout";
import { MetaGrid, SlimCard } from "./ui/cards";
import { useInterval } from "./hooks";
import { useRelayCtx } from "./relayContext";
import { TrustBadge } from "./views/AgentsView";
import { useEffect, useRef, useState } from "react";

/**
 * Auto-starts the dashboard auth flow when the relay is remote and no JWT is stored.
 * No button — polls silently until the agent completes the sign_dashboard_challenge MCP call.
 */
function RemoteAuthPanel() {
  const { viewerId, relayUrl, isLocalRelay, dashboardToken, setDashboardToken } = useRelayCtx();
  const [state, setState] = useState<"idle" | "pending" | "done" | "error">("idle");
  const [challenge, setChallenge] = useState<{ challenge_id: string; nonce: string } | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Auto-start challenge when remote relay + no JWT + we have an identity
  useEffect(() => {
    if (isLocalRelay || dashboardToken || !viewerId || state !== "idle") return;
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
  }, [isLocalRelay, dashboardToken, viewerId, state, setDashboardToken]);

  if (isLocalRelay || dashboardToken) return null;
  if (!viewerId) return null;

  return (
    <SlimCard title="Remote relay auth">
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

function SettlementRow({ s, viewerId }: { s: Settlement; viewerId: string }) {
  const won = s.winner_id === viewerId;
  const amount = won
    ? `+$${(s.winner_payout_cents / 100).toFixed(2)}`
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
  const { viewerId, relayOwner, relayId, relayUrl, requireNous, writeRequiresNous, isRelayOperator, isSpectator, agentCount, activeSessionCount, relayVersion } = useRelayCtx();
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
  const agents = agentsData?.agents ?? [];
  const registered = profile ?? agents.find((a) => a.agent_id === viewerId) ?? null;
  const displayName = registered?.display_name || registered?.agent_id || viewerId || "Spectator";

  const relayOwnerLabel = relayOwner ?? "standalone server";
  const totalEarned = mySettlements
    .filter((s) => s.winner_id === viewerId)
    .reduce((n, s) => n + s.winner_payout_cents, 0);

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
                ["Trust", <TrustBadge a={registered} />],
                ["Registered", new Date(registered.registered_at).toLocaleDateString()],
                ["Sessions", String(registered.session_count)],
                ["Wins", `${registered.win_count} (${registered.session_count > 0 ? Math.round((registered.win_count / registered.session_count) * 100) : 0}%)`],
                ["Stripe", registered.has_stripe ? "connected" : "not connected"],
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

        {/* My settlement history */}
        {!isSpectator && (
          <SlimCard
            title="Transaction history"
            action={
              totalEarned > 0 ? (
                <Text style={{ color: "var(--theme-focused-foreground)", fontSize: 12 }}>
                  +${(totalEarned / 100).toFixed(2)} earned
                </Text>
              ) : undefined
            }
          >
            {mySettlements.length === 0 ? (
              <Text className="e-faint">
                No settled transactions yet.{" "}
                {!registered?.has_stripe && "Connect Stripe to enable paid sessions."}
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
