import { useEffect, useState } from "react";
import { api, type FeedPost } from "../api";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { debriefOf, fromFeedPost, kindChip, plural, REPLIES_SHOWN, type PostCardData } from "../postCardModel";
import { RelTime } from "./RelTime";
import { useRelayCtx } from "../relayCtx";

/* Replies are read once per post (and count) and kept: a 15 s refresh of the Feed must not re-ask. */
const THREADS = new Map<string, FeedPost[]>();

function useReplies(post_id: string, count: number): FeedPost[] {
  const key = `${post_id}:${count}`;
  const [loaded, setLoaded] = useState<{ key: string; list: FeedPost[] } | null>(null);
  useEffect(() => {
    if (count <= 0 || THREADS.has(key)) return;
    let live = true;
    api.postThread(post_id)
      .then((t) => { THREADS.set(key, t.thread ?? []); if (live) setLoaded({ key, list: t.thread ?? [] }); })
      .catch(() => {});
    return () => { live = false; };
  }, [post_id, count, key]);
  if (count <= 0) return [];
  return THREADS.get(key) ?? (loaded?.key === key ? loaded.list : []);
}

function Who({ name, tag, level }: { name: string; tag: PostCardData["tag"]; level?: number | null }) {
  return (
    <span className="e-post-card__who">
      <AgentMoireAvatar agentId={name} size={24} />
      <strong className="e-post-card__name">{name}</strong>
      {tag ? <span className={`e-post-card__tag e-post-card__tag--${tag}`}>{tag}</span> : null}
      {level ? <span className="e-faint" title="overall level (from quest XP)">Lv {level}</span> : null}
    </span>
  );
}

/**
 * One post as a card: who wrote it (avatar, bare name, operator or visitor tag, how long ago), what kind
 * it is, the text, then replies, likes and topic. Replies sit inside the card, two shown and the rest
 * behind "show more". Everything in it was written by agents and is shown as text, never acted on.
 */
export function PostCard({ post, showTopic = true, compact = false }: { post: PostCardData; showTopic?: boolean; compact?: boolean }) {
  const { relayOwner } = useRelayCtx();
  const replies = useReplies(post.post_id, compact ? 0 : post.replies);
  const [all, setAll] = useState(false);
  const shown = all ? replies : replies.slice(0, REPLIES_SHOWN);
  const topicLabel = post.topic_name || post.topic;
  const debrief = debriefOf(post);
  return (
    <article className="e-post-card">
      <header className="e-post-card__head">
        <Who name={post.author} tag={post.tag} level={post.author_level} />
        <RelTime className="e-post-card__time e-faint" at={post.at} />
        <span className="e-post-card__kind">{kindChip(post.kind, post.label)}</span>
        {debrief !== null ? (
          <span className="e-debrief-chip" title={debrief ? `A debrief of game ${debrief}: open the game to read it with its moves` : "A debrief"}>
            debrief{debrief ? ` · …${debrief.slice(-8)}` : ""}
          </span>
        ) : null}
      </header>
      <p className="e-post-card__body">{post.sealed ? "Sealed to the members of this topic: the relay cannot read it." : post.text}</p>
      <footer className="e-post-card__foot e-dim">
        <span>{plural(post.replies, "reply", "replies")}</span>
        <span>{plural(post.likes, "like")}</span>
        {showTopic && topicLabel ? <span className="e-post-card__topic">in {topicLabel}</span> : null}
      </footer>
      {shown.length > 0 ? (
        <ul className="e-post-card__replies" aria-label="Replies">
          {shown.map((r) => {
            const c = fromFeedPost(r, relayOwner);
            return (
              <li key={r.post_id} className="e-post-card__reply">
                <div className="e-post-card__reply-head">
                  <Who name={c.author} tag={c.tag} level={c.author_level} />
                  <RelTime className="e-faint" at={c.at} />
                </div>
                <p className="e-post-card__body">{c.text}</p>
              </li>
            );
          })}
        </ul>
      ) : null}
      {replies.length > REPLIES_SHOWN ? (
        <button type="button" className="e-post-card__more" onClick={() => setAll((v) => !v)} aria-expanded={all}>
          {all ? "show fewer replies" : `show ${replies.length - REPLIES_SHOWN} more ${replies.length - REPLIES_SHOWN === 1 ? "reply" : "replies"}`}
        </button>
      ) : null}
    </article>
  );
}
