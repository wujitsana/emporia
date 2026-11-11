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
  /** The post's tags and what it replies to: a post tagged `debrief` shows a chip naming its game. */
  tags?: string[][];
  reply_to?: string | null;
  /** The thread's root as `post:<id>`, when this post is a reply. */
  root?: string | null;
}

/** The game a debrief post is about (`reply_to` = `session:<id>`, tagged `debrief`), or null for any other post. */
export function debriefOf(p: Pick<PostCardData, "tags" | "reply_to">): string | null {
  const tagged = (p.tags ?? []).some((t) => t[0] === "t" && t[1] === "debrief");
  const m = /^session:([^@#]+)/.exec(p.reply_to ?? "");
  return tagged ? (m ? m[1] : "") : null;
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
    likes: p.likes, replies: p.replies, topic: p.topic, tags: p.tags, reply_to: p.reply_to, root: p.root,
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
    topic: p.topic_slug, topic_name: p.topic_name, sealed: p.sealed, tags: p.tags,
  };
}

/**
 * The one way a time reads in a list (Home, Feed, Messages, the Arena's lists): "just now", "3m ago", "2h ago",
 * "yesterday", "3d ago", else the date; relative to `now`. Unparseable: the raw text. The exact time goes in a
 * hover title (`exactTime`, the RelTime component); detail views keep their absolute dates.
 */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso.slice(0, 16).replace("T", " ");
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 2) return "yesterday";
  if (s < 86400 * 14) return `${Math.floor(s / 86400)}d ago`;
  return new Date(t).toISOString().slice(0, 10);
}

/** The exact time, in the reader's own zone, for a hover title. */
export function exactTime(iso: string): string {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : new Date(t).toLocaleString();
}

/** How many replies a card shows before "show N more". */
export const REPLIES_SHOWN = 2;

export function plural(n: number, one: string, many: string = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
