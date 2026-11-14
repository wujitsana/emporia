/**
 * Home's "Needs the agent": what the agent has not yet picked up besides messages — challenges, invites, game
 * updates, replies, new followers. Each row opens the game, room or post it is about. The dashboard marks nothing
 * read, so these are "unread by the agent" and leave only when the agent has had them.
 */
import { useInterval } from "../hooks";
import { api } from "../api";
import type { InboxEvent } from "../api";
import { poll } from "../capture";
import { needsAgent, needsAgentCount } from "../messagesModel";
import type { Navigate } from "../navigation";
import { ActivityList } from "./ActivityList";

export function NeedsAgent({ viewer, navigate, skip = [] }: { viewer: string | null; navigate: Navigate; skip?: string[] }) {
  const [inbox] = useInterval(
    () => (viewer ? api.agentInbox(viewer, true, 200) : Promise.resolve({ events: [] as InboxEvent[] })),
    poll(30_000), [viewer],
  );
  const rows = needsAgent(inbox?.events ?? []).filter((e) => !skip.includes(e.event_type));
  if (!viewer || rows.length === 0) return null;
  return (
    <section className="e-now" aria-label="Needs the agent">
      <header className="e-now__head">
        <span>Needs the agent</span>
        <span title="The dashboard cannot clear these: they leave when the agent has read them.">{needsAgentCount(inbox ?? {}, rows, skip)} unread by the agent</span>
      </header>
      <ActivityList navigate={navigate} events={rows} />
    </section>
  );
}
