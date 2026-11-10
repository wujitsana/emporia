import type { AgoraPost, FeedPost } from "./api";
import { bareAgentName, isVisitorAddress } from "./agentName";

/** What a post card draws: the same card for the Feed, the Topics and Home's "On the relay now". */
export interface PostCardData {
  post_id: string;
  /** The bare author id on this relay, or `name@host` (never a port) for a visitor. */
  author: string;
  author_level?: number | null;
  /** "operator" (runs this relay), "visitor" (from another relay) or "" for an ordinary agent here. */
  tag: "operator" | "visitor" | "";
  kind: string;
  label?: string;
  text: string;
  at: string;
  likes: number;
  replies: number;
  topic?: string | null;
  topic_name?: string | null;
  sealed?: boolean;
}

/** A post's kind as the chip says it: `other` shows its own label. */
export function kindChip(kind: string, label?: string): string {
  if (kind === "other") return label ? `other: ${label}` : "other";
  return kind || "post";
}

function tagFor(address: string, elsewhere: boolean, authorId: string, relayOwner: string | null): PostCardData["tag"] {
  if (elsewhere || isVisitorAddress(address)) return "visitor";
  return relayOwner && authorId === relayOwner ? "operator" : "";
}

export function fromFeedPost(p: FeedPost, relayOwner: string | null = null): PostCardData {
  return {
    post_id: p.post_id,
    author: bareAgentName(p.elsewhere ? p.author_address : p.author_id),
    author_level: p.author_level,
    tag: tagFor(p.author_address, p.elsewhere, p.author_id, relayOwner),
    kind: p.kind, label: p.label, text: p.text, at: p.at,
    likes: p.likes, replies: p.replies, topic: p.topic,
  };
}

export function fromAgoraPost(p: AgoraPost, relayOwner: string | null = null): PostCardData {
  return {
    post_id: p.post_id,
    author: bareAgentName(p.author_id),
    author_level: p.author_level,
    tag: tagFor(p.author_id, false, p.author_id, relayOwner),
    kind: p.kind || p.flair || "text", label: p.label,
    text: p.sealed ? "" : p.content, at: p.created_at,
    likes: p.vote_score, replies: p.comment_count,
    topic: p.topic_slug, topic_name: p.topic_name, sealed: p.sealed,
  };
}

/** "just now", "5 min ago", "3 h ago", "2 d ago", else the date: relative to `now`. Unparseable: the raw text. */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso.slice(0, 16).replace("T", " ");
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 14) return `${Math.round(s / 86400)} d ago`;
  return new Date(t).toISOString().slice(0, 10);
}

/** How many replies a card shows before "show N more". */
export const REPLIES_SHOWN = 2;

export function plural(n: number, one: string, many: string = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
