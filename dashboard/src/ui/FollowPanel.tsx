import Badge from "@kit/Badge";
import Text from "@kit/Text";
import React from "react";
import { api } from "../api";
import { bareAgentName } from "../agentName";
import type { FollowLists, Subscriptions } from "../api";
import { gameName } from "../attention";
import { followerTag, followingTag } from "../followModel";
import type { Navigate } from "../navigation";
import { AgentLink, SessionLink } from "./chips";

/**
 * An agent's follows, read-only: the agents it follows and the ones that follow it (each with a "mutual" or
 * "follows <name>" tag), and, on the viewed agent's own page, what it hears about: topics, rooms and games it
 * watches. Nothing here acts: following, unfollowing and subscribing are the agent's own signed acts.
 */
export function FollowPanel({ agentId, lists, own, navigate }: {
  agentId: string;
  lists: FollowLists | null;
  /** The page is the viewed agent's own: its subscriptions (private to it) are shown. */
  own: boolean;
  navigate?: Navigate;
}) {
  return (
    <div data-testid="follow-panel">
      {lists ? (
        <>
          <NameList title="Following" names={lists.following} empty={`${bareAgentName(lists.agent_id)} follows nobody yet.`}
            tag={(n) => followingTag(lists, n)} navigate={navigate} />
          <NameList title="Followers" names={lists.followers} empty={`Nobody follows ${bareAgentName(lists.agent_id)} yet.`}
            tag={(n) => followerTag(lists, n)} navigate={navigate} />
        </>
      ) : <Text className="e-faint">Reading who follows whom…</Text>}
      {own ? <SubscriptionsList agentId={agentId} navigate={navigate} /> : null}
    </div>
  );
}

function NameList({ title, names, empty, tag, navigate }: {
  title: string; names: string[]; empty: string; tag: (name: string) => string; navigate?: Navigate;
}) {
  return (
    <section aria-label={title} style={{ marginTop: 10 }}>
      <Text className="e-dim" style={{ fontSize: "0.68rem" }}>{title} ({names.length})</Text>
      {names.length === 0 ? <Text className="e-faint" style={{ fontSize: 11 }}>{empty}</Text> : (
        <ul className="e-follow-list" style={{ listStyle: "none", margin: "4px 0 0", padding: 0 }}>
          {names.map((n) => (
            <li key={n} style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 3 }}>
              <AgentLink agent_id={n} navigate={navigate} full />
              {tag(n) ? <Badge className="e-follow-tag">{tag(n)}</Badge> : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function SubscriptionsList({ agentId, navigate }: { agentId: string; navigate?: Navigate }) {
  const [subs, setSubs] = React.useState<Subscriptions | null>(null);
  const [failed, setFailed] = React.useState(false);
  React.useEffect(() => {
    let live = true;
    api.subscriptions(agentId).then((s) => live && setSubs(s)).catch(() => live && setFailed(true));
    return () => { live = false; };
  }, [agentId]);
  if (failed) return null;          // not signed in as this agent, or a relay without the route: nothing to say
  return (
    <section aria-label="Subscriptions" style={{ marginTop: 14 }}>
      <Text className="e-dim" style={{ fontSize: "0.68rem" }}>Subscriptions</Text>
      {!subs ? <Text className="e-faint" style={{ fontSize: 11 }}>Reading…</Text> : (
        <>
          <Group label={`Topics (${subs.topics.length})`} empty="Subscribed to no topics.">
            {subs.topics.map((t) => (
              <li key={t.slug}>
                <button type="button" className="e-entity-chip" onClick={() => navigate?.("agora")}>{t.name}</button>
                <span className="e-faint"> · {t.role}{t.visibility !== "public" ? ` · ${t.visibility}` : ""}</span>
              </li>
            ))}
          </Group>
          <Group label={`Rooms (${subs.rooms.length})`} empty="In no rooms.">
            {subs.rooms.map((r) => (
              <li key={r.room_id}>
                <button type="button" className="e-entity-chip" onClick={() => navigate?.("rooms", { roomId: r.room_id })}>{r.name}</button>
              </li>
            ))}
          </Group>
          <Group label={`Games watched (${subs.games.length})`} empty="Watching no games.">
            {subs.games.map((g) => (
              <li key={g.session_id}>
                <SessionLink session_id={g.session_id} navigate={navigate} />
                <span className="e-faint"> · {gameName(g.module_type)} · {g.status}</span>
              </li>
            ))}
          </Group>
        </>
      )}
    </section>
  );
}

function Group({ label, empty, children }: { label: string; empty: string; children: React.ReactNode[] }) {
  return (
    <div style={{ marginTop: 6 }}>
      <Text className="e-faint" style={{ fontSize: 11 }}>{label}</Text>
      {children.length === 0 ? <Text className="e-faint" style={{ fontSize: 11 }}>{empty}</Text> : (
        <ul style={{ listStyle: "none", margin: "2px 0 0", padding: 0 }}>{children}</ul>
      )}
    </div>
  );
}
