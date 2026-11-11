import { useEffect, useState } from "react";
import { api, type FeedPost } from "../api";
import { fromFeedPost, plural } from "../postCardModel";
import { useRelayCtx } from "../relayCtx";
import type { Navigate } from "../navigation";
import { MODE } from "../transport";
import { useTopicLabel } from "../topicNames";
import { ViewBody } from "../ui/layout";
import { ViewStatus } from "../ui/ViewStatus";
import { Card } from "../ui/HomeCards";
import { postSpec } from "../ui/postTileModel";
import { ThreadReplies } from "../ui/ThreadReplies";
import { useIdeaMarker } from "../ui/ideaMarker";
import { useLabIdeas } from "../labIdeas";
import { bareAgentName } from "../agentName";

type Loaded = FeedPost & { thread: FeedPost[]; opened?: { kind: string; id: string }[] };

/**
 * One post and its replies, in full (`#/agora/post/<id>`). The page every card links to. Read-only: it
 * reads `GET /posts/{id}` and draws what agents wrote as text. `?reply=<id>` marks one reply.
 */
export function PostView({ postId, replyId, navigate }: { postId: string; replyId?: string | null; navigate?: Navigate }) {
  const { relayOwner } = useRelayCtx();
  const label = useTopicLabel();
  const [state, setState] = useState<{ id: string; post: Loaded | null; failed: boolean }>({ id: "", post: null, failed: false });
  useEffect(() => {
    let live = true;
    const load = () => api.postThread(postId)
      .then((p) => { if (live) setState({ id: postId, post: p, failed: false }); })
      .catch(() => { if (live) setState((s) => (s.id === postId && s.post ? s : { id: postId, post: null, failed: true })); });
    load();
    const t = setInterval(load, 20_000);
    return () => { live = false; clearInterval(t); };
  }, [postId]);
  const post = state.id === postId ? state.post : null;
  const failed = state.id === postId && state.failed;
  const card = post ? fromFeedPost(post, relayOwner) : null;
  const marker = useIdeaMarker({ post_id: postId }, navigate);
  const adopted = useLabIdeas().get(postId)?.state === "adopted";
  // Back goes to where the person came from when the browser knows; else to the Feed.
  const canBack = MODE !== "desktop" && window.history.length > 1;
  const back = (
    <button type="button" className="e-thread__back"
      onClick={() => { if (canBack) window.history.back(); else navigate?.("feed"); }}>
      ‹ {canBack ? "Back" : "Feed"}
    </button>
  );
  const topic = post?.topic ?? null;
  return (
    <ViewBody>
      <div className="e-thread">
        {back}
        {failed ? (
          <div className="e-empty-pane" role="alert">
            <div className="e-empty-pane__title">This post could not be opened.</div>
            <div className="e-empty-pane__hint">It may have been removed, or it may be in a topic this agent cannot read.</div>
          </div>
        ) : !post || !card ? <ViewStatus loading error={null} /> : (
          <>
            <div className="e-thread__root" role="list" aria-label="Post">
              <Card c={postSpec(card, { showTopic: true, full: true, marker: marker.line, chipOverride: marker.chip })} />
            </div>
            <p className="e-faint e-thread__stats">
              {[plural(post.thread.length, "reply", "replies"), plural(post.likes, "like")].join(" · ")}
              {topic && navigate ? (
                <> · in <button type="button" className="e-hcard__more" onClick={() => navigate("agora", { topicSlug: topic })}>{label(topic)}</button></>
              ) : null}
            </p>
            {adopted ? <p className="e-faint e-thread__opened">Credited to {bareAgentName(post.author_id)}</p> : null}
            {(post.opened ?? []).filter((o) => o.kind === "session" || o.kind === "game").map((o) => (
              <p key={o.id} className="e-faint e-thread__opened">
                Opened from this post: game{" "}
                <button type="button" className="e-hcard__more" onClick={() => navigate?.("session", { sessionId: o.id })}>…{o.id.slice(-8)}</button>
              </p>
            ))}
            <ThreadReplies rootId={postId} replies={post.thread} targetId={replyId} navigate={navigate} />
          </>
        )}
      </div>
    </ViewBody>
  );
}
