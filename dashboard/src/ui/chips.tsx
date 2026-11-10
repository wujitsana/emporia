/**
 * Clickable entity chips — consistent cross-view navigation.
 * Pass navigate to make them interactive; omit for plain display.
 */
import React from "react";
import type { Navigate } from "../navigation";
import { bareAgentName } from "../agentName";

interface AgentLinkProps {
  agent_id: string;
  navigate?: Navigate;
  /** If true, show full id; default shows last 12 chars */
  full?: boolean;
  className?: string;
  /** The agent's overall level (level:v1), shown beside the name; a label, never a ranking. */
  level?: number | null;
}

export function AgentLink({ agent_id, navigate, full, className, level }: AgentLinkProps) {
  const bare = bareAgentName(agent_id);
  const label = full ? bare : (bare.length > 14 ? `…${bare.slice(-12)}` : bare);
  const lv = level ? <span className="e-faint" title="overall level (from quest XP)"> · Lv {level}</span> : null;
  if (!navigate) {
    return <span className={`e-entity-chip e-entity-chip--inert ${className ?? ""}`}>{label}{lv}</span>;
  }
  return (
    <button
      type="button"
      className={`e-entity-chip e-entity-chip--agent ${className ?? ""}`}
      title={agent_id}
      onClick={(e) => { e.stopPropagation(); navigate("agents", { agentId: agent_id }); }}
    >
      {label}{lv}
    </button>
  );
}

interface SessionLinkProps {
  session_id: string;
  navigate?: Navigate;
  className?: string;
}

/** A small muted label beside a line's time: "opened here", "sealed", "model gpt-5.6": facts about the line, not part of its words. */
export function QuietChip({ children, title }: { children: React.ReactNode; title?: string }) {
  return <span className="e-quiet-chip" title={title}>{children}</span>;
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
      onClick={(e) => { e.stopPropagation(); navigate("session", { sessionId: session_id }); }}
    >
      {label}
    </button>
  );
}
