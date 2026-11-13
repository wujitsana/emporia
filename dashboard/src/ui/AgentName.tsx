import type { KeyboardEvent, MouseEvent, ReactNode } from "react";
import { useAgentNav } from "../agentNavCtx";

/** An agent's name as a link to its page (`role="link"`, so it can sit inside a clickable row or card). */
export function AgentName({ id, children, className }: { id: string; children: ReactNode; className?: string }) {
  const nav = useAgentNav();
  if (!nav) return <span className={className}>{children}</span>;
  const open = (e: MouseEvent | KeyboardEvent) => { e.stopPropagation(); nav("agents", { agentId: id }); };
  return (
    <span className={`e-agent-link ${className ?? ""}`} role="link" tabIndex={0} title={`Open ${id}'s page`}
      onClick={open} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); open(e); } }}>
      {children}
    </span>
  );
}
