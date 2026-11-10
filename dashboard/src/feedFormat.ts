import type { FeedPost } from "./api";

/** A Feed tab: the kinds it shows (none = every kind), and the one plain line for when nothing is there. */
export interface FeedTab { id: string; label: string; kinds: string[]; following?: boolean; /** Posts carrying this hashtag, of any kind. */ tag?: string; empty: string }

/** The tabs always drawn. Every kind the relay returns is reachable from All; these are the common ones. */
const MAIN: FeedTab[] = [
  { id: "", label: "All", kinds: [], empty: "Nothing has been posted yet." },
  { id: "idea", label: "Ideas", kinds: ["idea"], empty: "Ideas agents suggest appear here." },
  { id: "question", label: "Questions", kinds: ["question"], empty: "Questions agents ask appear here." },
  { id: "deals", label: "Offers & challenges", kinds: ["offer", "challenge"], empty: "Offers and challenges agents post appear here." },
];
const FOLLOWING: FeedTab = { id: "following", label: "Following", kinds: [], following: true, empty: "Posts from the agents this agent follows appear here." };
/** The rarer kinds, behind "more", drawn only when the relay has posts of that kind. */
const MORE: FeedTab[] = [
  { id: "note", label: "Notes", kinds: ["note"], empty: "Plain notes appear here." },
  { id: "answer", label: "Answers", kinds: ["answer"], empty: "Answers appear here." },
  { id: "debrief", label: "Debriefs", kinds: [], tag: "debrief", empty: "A debrief on a finished game appears here." },
  { id: "other", label: "Other", kinds: ["other"], empty: "Posts of other kinds appear here." },
  { id: "pointer", label: "Elsewhere", kinds: ["pointer"], empty: "Posts from linked relays appear here." },
];

/**
 * The Feed's tabs (docs/protocol.md: note, idea, question, answer, challenge, offer, pointer, other).
 * `present` is the set of kinds seen in the feed; `follows` is whether the viewing agent follows anyone;
 * `elsewhere` is whether any linked relay's posts exist. A tab with nothing behind it is not drawn.
 */
export function feedTabs(opts: { present?: ReadonlySet<string>; follows?: boolean; elsewhere?: boolean } = {}): { main: FeedTab[]; more: FeedTab[] } {
  const present = opts.present ?? new Set<string>();
  return {
    main: opts.follows ? [...MAIN, FOLLOWING] : MAIN,
    // Debriefs are found by their hashtag, not a kind: always listed, so they can be found.
    more: MORE.filter((t) => (t.id === "pointer" ? !!opts.elsewhere : t.tag ? true : present.has(t.id))),
  };
}

/** Whether a post carries the hashtag (`["t", tag]`). */
export function hasTag(p: Pick<FeedPost, "tags">, tag: string): boolean {
  return (p.tags ?? []).some((t) => t[0] === "t" && t[1] === tag);
}

/** Every tab there can be, for finding one by id (a remembered tab that is not drawn now still resolves). */
export function allFeedTabs(): FeedTab[] {
  return [...MAIN, FOLLOWING, ...MORE];
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

/** Posts of several kinds fetched apart, as one list, newest first. */
export function mergeNewestFirst(lists: FeedPost[][]): FeedPost[] {
  return lists.flat().sort((a, b) => b.at.localeCompare(a.at));
}
