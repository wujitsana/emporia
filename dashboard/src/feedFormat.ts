import type { FeedPost } from "./api";

/** The Feed's tabs are kinds (docs/protocol.md): an empty kind is All. */
export function feedTabs(): { id: string; label: string; kind: string }[] {
  return [
    { id: "", label: "All", kind: "" },
    { id: "idea", label: "Ideas", kind: "idea" },
    { id: "question", label: "Questions", kind: "question" },
    { id: "challenge", label: "Challenges", kind: "challenge" },
    { id: "offer", label: "Offers", kind: "offer" },
    { id: "pointer", label: "Discover", kind: "pointer" },
    { id: "other", label: "Other", kind: "other" },
  ];
}

/** A post's address as text, `post:ID`, the same shape a reply or a reaction names it by. */
export function postRef(id: string): string {
  return `post:${id}`;
}

/** Tags as plain text: `#hashtag`, `ref kind:id`, `tool ecosystem/name`, anything else as written. */
export function tagSummary(tags: string[][]): string {
  return tags
    .map((t) => {
      if (t[0] === "t") return `#${t[1]}`;
      if (t[0] === "ref") return `ref ${t[1]}:${t[2]}${t[3] ? `@${t[3]}` : ""}`;
      if (t[0] === "tool") return `tool ${t[1]}/${t[2]}`;
      if (t[0] === "p") return `@${t[1]}`;
      return t.join(" ");
    })
    .join(" ");
}

/** Likes (the one that counts) first, then every other value with its count, as text. */
export function reactionSummary(p: Pick<FeedPost, "likes" | "reactions">): string {
  const others = Object.entries(p.reactions ?? {}).map(([v, n]) => `${v} ${n}`);
  return [`${p.likes} likes`, ...others].join(" · ");
}
