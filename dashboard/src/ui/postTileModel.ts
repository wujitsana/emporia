import type { ReactNode } from "react";
import { debriefOf, plural, type PostCardData } from "../postCardModel";
import { clip, type CardSpec } from "./homeCardsModel";

/** The card's headline: the kind in a word; `other` is its label; a debrief says so. */
export function postHead(post: Pick<PostCardData, "kind" | "label" | "tags" | "reply_to">): string {
  if (debriefOf(post) !== null) return "Debrief";
  if (post.kind === "other") return post.label || "other";
  return post.kind === "note" || !post.kind ? "Post" : post.kind;
}

export function postSpec(post: PostCardData, opts: { showTopic?: boolean; onOpen?: () => void; extra?: ReactNode } = {}): CardSpec {
  const topic = post.topic_name || post.topic || "";
  const showTopic = opts.showTopic ?? true;
  const who = `${post.author}${post.tag ? ` (${post.tag})` : ""}`;
  return {
    key: post.post_id,
    meta: `post${showTopic && topic ? ` · ${clip(topic, 28)}` : ""}`,
    at: post.at,
    chip: post.kind === "other" ? "OTHER" : (post.kind || "post").toUpperCase(),
    head: postHead(post),
    body: post.sealed ? "Sealed to the members of this topic: the relay cannot read it." : post.text,
    caption: who,
    agent: post.author,
    foot: [plural(post.replies, "reply", "replies"), plural(post.likes, "like")].join(" · "),
    onOpen: opts.onOpen,
    theirs: true,
    extra: opts.extra,
  };
}
