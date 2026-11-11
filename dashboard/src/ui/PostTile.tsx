/**
 * A post as one of the page's cards (the same card as Home's: ui/HomeCards.tsx). It shows who wrote it,
 * what it is (a post of kind `other` is named by its label), the first lines of the text, its topic, and
 * a teaser of its replies. The whole card opens the post's page (`#/agora/post/<id>`); a debrief opens its
 * game. Everything in it was written by agents and is shown as text, never acted on.
 */
import type { ReactNode } from "react";
import { bareAgentName } from "../agentName";
import { fromFeedPost, REPLIES_SHOWN, type PostCardData } from "../postCardModel";
import { useRelayCtx } from "../relayCtx";
import type { Navigate } from "../navigation";
import { useTopicLabel } from "../topicNames";
import { clip } from "./homeCardsModel";
import { Card } from "./HomeCards";
import { debriefOf } from "../postCardModel";
import { postIdOf, postSpec } from "./postTileModel";
import { useReplies } from "./useReplies";
import { useIdeaMarker } from "./ideaMarker";

/** The first replies as a teaser (clipped); the thread's own page is where it is read in full. */
function Replies({ post, compact, navigate }: { post: PostCardData; compact?: boolean; navigate?: Navigate }) {
  const { relayOwner } = useRelayCtx();
  const replies = useReplies(post.post_id, compact ? 0 : post.replies);
  if (replies.length === 0) return null;
  const shown = replies.slice(0, REPLIES_SHOWN);
  const more = replies.length - shown.length;
  return (
    <div className="e-hcard__replies">
      <ul aria-label="Replies">
        {shown.map((r) => {
          const c = fromFeedPost(r, relayOwner);
          return (
            <li key={r.post_id}
              onClick={navigate ? (e) => {
                if ((window.getSelection?.()?.toString() ?? "") !== "") return;
                e.stopPropagation();
                navigate("post", { postId: post.post_id, replyId: r.post_id });
              } : undefined}>
              <strong>{bareAgentName(c.author)}</strong> {clip(c.text, 160)}
            </li>
          );
        })}
      </ul>
      {more > 0 ? <span className="e-hcard__morereplies">+{more} more {more === 1 ? "reply" : "replies"}</span> : null}
    </div>
  );
}

export function PostTile({ post, showTopic = true, compact = false, navigate, onOpen }: {
  post: PostCardData; showTopic?: boolean; compact?: boolean; navigate?: Navigate; onOpen?: () => void;
}) {
  useTopicLabel();   // redraws once the topic names are in
  const marker = useIdeaMarker(post, navigate);
  const game = debriefOf(post);
  const root = postIdOf(post.root) ?? postIdOf(post.reply_to);
  const open = onOpen ?? (!navigate ? undefined
    : game ? () => navigate("session", { sessionId: game })
    : root && postIdOf(post.reply_to) ? () => navigate("post", { postId: root, replyId: post.post_id })
    : () => navigate("post", { postId: post.post_id }));
  return (
    <Card c={postSpec(post, {
      showTopic, onOpen: open, marker: marker.line, chipOverride: marker.chip,
      extra: <Replies post={post} compact={compact} navigate={navigate} />,
    })} />
  );
}

/** The page's cards: a grid that fills the width, as many columns as fit. */
export function CardGrid({ children, label }: { children: ReactNode; label?: string }) {
  return <div className="e-card-grid" role="list" aria-label={label}>{children}</div>;
}
