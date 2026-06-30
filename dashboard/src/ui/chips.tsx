/**
 * Clickable entity chips — consistent cross-view navigation.
 * Pass navigate to make them interactive; omit for plain display.
 */
import React from "react";
import type { Navigate } from "../navigation";

interface AgentLinkProps {
  agent_id: string;
  navigate?: Navigate;
  /** If true, show full id; default shows last 12 chars */
  full?: boolean;
  className?: string;
}

export function AgentLink({ agent_id, navigate, full, className }: AgentLinkProps) {
  const label = full ? agent_id : (agent_id.length > 14 ? `…${agent_id.slice(-12)}` : agent_id);
  if (!navigate) {
    return <span className={`e-entity-chip e-entity-chip--inert ${className ?? ""}`}>{label}</span>;
  }
  return (
    <button
      type="button"
      className={`e-entity-chip e-entity-chip--agent ${className ?? ""}`}
      title={agent_id}
      onClick={(e) => { e.stopPropagation(); navigate("agents", { agentId: agent_id }); }}
    >
      {label}
    </button>
  );
}

interface SessionLinkProps {
  session_id: string;
  navigate?: Navigate;
  className?: string;
}

export function SessionLink({ session_id, navigate, className }: SessionLinkProps) {
  const label = `…${session_id.slice(-10)}`;
  if (!navigate) {
    return <span className={`e-entity-chip e-entity-chip--inert ${className ?? ""}`}>{label}</span>;
  }
  return (
    <button
      type="button"
      className={`e-entity-chip e-entity-chip--session ${className ?? ""}`}
      title={session_id}
      onClick={(e) => { e.stopPropagation(); navigate("sessions", { sessionId: session_id }); }}
    >
      {label}
    </button>
  );
}
