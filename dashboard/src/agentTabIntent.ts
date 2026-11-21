/** A one-shot note from a list to the agent page it is about to open: "open it on this tab". The page takes it when
 *  it mounts, once; without a note it starts on its first tab. (The agent route names the agent only, not the tab.) */
let pending: { agentId: string; tab: string } | null = null;

export function requestAgentTab(agentId: string, tab: string): void {
  pending = { agentId, tab };
}

export function takeAgentTab(agentId: string): string | null {
  const p = pending;
  pending = null;
  return p && p.agentId === agentId ? p.tab : null;
}

/** Whether a click in a list row landed on the 8004 mark (`ChainMark`). */
export const opensIdentity = (target: EventTarget | null): boolean =>
  target instanceof Element && target.closest('[data-testid="chain-mark"]') !== null;
