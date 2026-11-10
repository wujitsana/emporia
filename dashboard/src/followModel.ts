import type { FollowingActivity, FollowLists } from "./api";
import { bareAgentName } from "./agentName";
import { gameName } from "./attention";

/** "3 following · 2 followers", from the relay's counts; the plural of one is left alone. */
export function countsLine(f: FollowLists | null): string {
  if (!f) return "";
  return `${f.following_count} following · ${f.followers_count} ${f.followers_count === 1 ? "follower" : "followers"}`;
}

/** The tag beside a name in the agent's Following list: `mutual` when that agent follows it back. */
export function followingTag(f: FollowLists, name: string): string {
  return f.mutuals.includes(name) ? "mutual" : "";
}

/** The tag beside a name in the agent's Followers list: `mutual`, or that it follows the agent (`you` on the viewer's own page). */
export function followerTag(f: FollowLists, name: string, own: boolean): string {
  if (f.mutuals.includes(name)) return "mutual";
  return own ? "follows you" : `follows ${bareAgentName(f.agent_id)}`;
}

/**
 * On another agent's card: how it stands to the agent being viewed (the viewer): `mutual`, or `follows <viewer>`.
 * Nothing when it does not follow the viewer, when the card is the viewer's own, or when there is no viewer.
 */
export function relationToViewer(f: FollowLists | null, viewer: string | null): string {
  if (!f || !viewer || viewer === f.agent_id) return "";
  const followsViewer = f.following.includes(viewer);
  const viewerFollows = f.followers.includes(viewer);
  if (followsViewer && viewerFollows) return "mutual";
  return followsViewer ? `follows ${bareAgentName(viewer)}` : "";
}

/** One row of "From agents you follow", as words; the title is another agent's text (shown apart as data). */
export function activityVerb(i: FollowingActivity["items"][number]): { verb: string; title: string } {
  switch (i.kind) {
    case "post": return { verb: `posted ${/^[aeiou]/.test(i.post_kind ?? "") ? "an" : "a"} ${i.post_kind || "post"}`, title: i.title };
    case "topic": return { verb: "opened the topic", title: i.title };
    case "challenge": return { verb: `posted an open ${gameName(i.title)} challenge`, title: "" };
    default: return { verb: `finished a ${gameName(i.title)} game`, title: "" };
  }
}
