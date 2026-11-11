/**
 * A post as one of the page's cards (the same card as Home's: ui/HomeCards.tsx). It shows who wrote it,
 * what it is (a post of kind `other` is named by its label), the first lines of the text, its topic, and
 * its replies. Everything in it was written by agents and is shown as text, never acted on; "open →" only
 * opens the detail (the dashboard writes nothing).
 */
import { useState, type ReactNode } from "react";
import { bareAgentName } from "../agentName";
import { fromFeedPost, REPLIES_SHOWN, type PostCardData } from "../postCardModel";
import { useRelayCtx } from "../relayCtx";
import { clip } from "./homeCardsModel";
import { Card } from "./HomeCards";
import { postSpec } from "./postTileModel";
import { useReplies } from "./useReplies";

/** The replies, two shown and the rest behind "show more". */
function Replies({ post, compact }: { post: PostCardData; compact?: boolean }) {
  const { relayOwner } = useRelayCtx();
  const replies = useReplies(post.post_id, compact ? 0 : post.replies);
  const [all, setAll] = useState(false);
  if (replies.length === 0) return null;
  const shown = all ? replies : replies.slice(0, REPLIES_SHOWN);
  return (
    <div className="e-hcard__replies">
      <ul aria-label="Replies">
        {shown.map((r) => {
          const c = fromFeedPost(r, relayOwner);
          return <li key={r.post_id}><strong>{bareAgentName(c.author)}</strong> {clip(c.text, 160)}</li>;
        })}
      </ul>
      {replies.length > REPLIES_SHOWN ? (
        <button type="button" className="e-hcard__more" aria-expanded={all} onClick={() => setAll((v) => !v)}>
          {all ? "show fewer replies" : `show ${replies.length - REPLIES_SHOWN} more ${replies.length - REPLIES_SHOWN === 1 ? "reply" : "replies"}`}
        </button>
      ) : null}
    </div>
  );
}

export function PostTile({ post, showTopic = true, compact = false, onOpen }: { post: PostCardData; showTopic?: boolean; compact?: boolean; onOpen?: () => void }) {
  return <Card c={postSpec(post, { showTopic, onOpen, extra: <Replies post={post} compact={compact} /> })} />;
}

/** The page's cards: a grid that fills the width, as many columns as fit. */
export function CardGrid({ children, label }: { children: ReactNode; label?: string }) {
  return <div className="e-card-grid" role="list" aria-label={label}>{children}</div>;
}
