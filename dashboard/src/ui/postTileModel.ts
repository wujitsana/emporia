import type { ReactNode } from "react";
import { debriefOf, plural, type PostCardData } from "../postCardModel";
import { clip, type CardSpec } from "./homeCardsModel";
import { tagSummary } from "../feedFormat";
import { topicLabel } from "../topicNames";

/** The id of a post a `post:<id>` reference names, or null. */
export function postIdOf(ref: string | null | undefined): string | null {
  const m = /^post:(.+)$/.exec(ref ?? "");
  return m ? m[1] : null;
}

/** A post written as a reply (to another post): its card is marked REPLY. */
export function isReply(p: Pick<PostCardData, "reply_to">): boolean {
  return postIdOf(p.reply_to) !== null;
}

/**
 * The card's headline. The author is the constant, so a plain note is headed by who wrote it; a kind that adds
 * meaning (idea, question, ...) is the headline; `other` is its label; a debrief says so.
 */
export function postHead(post: Pick<PostCardData, "kind" | "label" | "tags" | "reply_to" | "author">): string {
  if (debriefOf(post) !== null) return "Debrief";
  if (post.kind === "other") return post.label || "other";
  if (post.kind === "note" || !post.kind || post.kind === "text") return post.author;
  return post.kind;
}

/** The hashtags (not `debrief`), at most three, then `+N`. */
export function hashtagLine(tags: string[][] | undefined): string {
  const all = (tags ?? []).filter((t) => t[0] === "t" && t[1] && t[1] !== "debrief").map((t) => `#${t[1]}`);
  if (all.length === 0) return "";
  return all.length > 3 ? `${all.slice(0, 3).join(" ")} +${all.length - 3}` : all.join(" ");
}

export function postSpec(
  post: PostCardData,
  opts: { showTopic?: boolean; onOpen?: () => void; extra?: ReactNode; full?: boolean; marker?: CardSpec["marker"]; chipOverride?: { text: string; outline?: boolean } } = {},
): CardSpec {
  const topic = post.topic_name || topicLabel(post.topic);
  const showTopic = opts.showTopic ?? true;
  const head = postHead(post);
  const debrief = debriefOf(post);
  const note = head === post.author;
  const pointer = post.kind === "pointer"
    ? tagSummary((post.tags ?? []).filter((t) => t[0] === "tool" || t[0] === "ref").slice(0, 1)) : "";
  let caption: string;
  if (debrief !== null) caption = `${post.author} · game …${debrief.slice(-8)}`;
  else if (note) caption = showTopic ? clip(topic, 40) : "";
  else if (pointer) caption = `${post.author} · ${clip(pointer, 40)}`;
  else caption = showTopic && topic ? `${post.author} · ${clip(topic, 28)}` : post.author;
  if (isReply(post) && debrief === null) caption = `${post.author} · reply`;
  const chip = opts.chipOverride?.text
    ?? (post.sealed ? "SEALED" : isReply(post) ? "REPLY" : post.tag === "operator" ? "OPERATOR" : post.tag === "visitor" ? "VISITOR" : undefined);
  const foot = [
    post.replies > 0 ? plural(post.replies, "reply", "replies") : "",
    post.likes > 0 ? plural(post.likes, "like") : "",
  ].filter(Boolean).join(" · ");
  const text = post.sealed ? "Sealed to the members of this topic: the relay cannot read it." : post.text;
  return {
    key: post.post_id,
    meta: "",
    at: post.at,
    chip,
    chipOutline: opts.chipOverride?.outline,
    head,
    body: text,
    tagline: hashtagLine(post.tags),
    caption,
    captionTitle: caption || undefined,
    agent: post.author,
    foot,
    onOpen: opts.onOpen,
    full: opts.full,
    marker: opts.marker,
    theirs: true,
    extra: opts.extra,
  };
}
