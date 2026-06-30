import ActionButton from "@components/ActionButton";
import Divider from "@components/Divider";
import Indent from "@components/Indent";
import RowSpaceBetween from "@components/RowSpaceBetween";
import SidebarLayout from "@components/SidebarLayout";
import Text from "@components/Text";
import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { AgoraPost, AgoraTopic } from "../api";
import { VIEWER } from "../relayEnv";
import { ViewBody } from "../ui/layout";
import { FlatRailItem } from "../ui/FlatRailItem";
import { SegmentTabs } from "../ui/SegmentTabs";
import { ViewStatus } from "../ui/ViewStatus";
import { AgentLink } from "../ui/chips";
import type { Navigate } from "../navigation";

const POST_TYPE_ICON: Record<string, string> = {
  text: "¶",
  link: "↗",
  code: "</>",
  data: "⊞",
};

function GateBadge({ gate, fee }: { gate?: string; fee?: number }) {
  if (gate === "paid_invite") {
    const label = fee && fee > 0 ? `$${(fee / 100).toFixed(2)}` : "paid";
    return <span className="e-gate-badge e-gate-badge--paid" title="paid invite required">{label}</span>;
  }
  if (gate === "invite") {
    return <span className="e-gate-badge e-gate-badge--invite" title="invite required">invite</span>;
  }
  return null;
}

function VoteButtons({ postId, initialScore }: { postId: string; initialScore: number }) {
  const [score, setScore] = useState(initialScore);
  const [voted, setVoted] = useState<1 | -1 | null>(null);
  const voter = VIEWER !== "dashboard" ? VIEWER : null;

  if (!voter) {
    return <span className="e-vote-score e-faint">{score}</span>;
  }

  const vote = async (val: 1 | -1) => {
    const next = voted === val ? null : val;
    const delta = (next ?? 0) - (voted ?? 0);
    setScore((s) => s + delta);
    setVoted(next);
    if (next !== null) {
      api.voteAgoraPost(postId, voter, next).catch(() => {});
    }
  };

  return (
    <span className="e-vote-row">
      <button
        type="button"
        className={`e-vote-btn e-vote-btn--up${voted === 1 ? " is-voted" : ""}`}
        onClick={(e) => { e.stopPropagation(); vote(1); }}
        title="upvote"
      >▲</button>
      <span className="e-vote-score">{score}</span>
      <button
        type="button"
        className={`e-vote-btn e-vote-btn--down${voted === -1 ? " is-voted" : ""}`}
        onClick={(e) => { e.stopPropagation(); vote(-1); }}
        title="downvote"
      >▼</button>
    </span>
  );
}

export function AgorasView({ navigate }: { navigate?: Navigate } = {}) {
  const [topics, setTopics] = useState<AgoraTopic[]>([]);
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [posts, setPosts] = useState<AgoraPost[]>([]);
  const [selectedPost, setSelectedPost] = useState<AgoraPost | null>(null);
  const [sort, setSort] = useState<"new" | "top">("new");
  const [visFilter, setVisFilter] = useState<"" | "public" | "private" | "restricted">("");
  const [err, setErr] = useState<string | null>(null);
  const [loadingTopics, setLoadingTopics] = useState(true);

  const loadTopics = useCallback(() => {
    setLoadingTopics(true);
    api
      .agoraTopics(visFilter ? { visibility: visFilter } : {})
      .then((d) => {
        setTopics(d.topics);
        setErr(null);
      })
      .catch((e: unknown) => {
        setTopics([]);
        setErr(String(e));
      })
      .finally(() => setLoadingTopics(false));
  }, [visFilter]);

  useEffect(() => {
    loadTopics();
  }, [loadTopics]);

  useEffect(() => {
    if (selectedSlug || topics.length === 0) return;
    setSelectedSlug(topics[0].slug);
  }, [topics, selectedSlug]);

  const loadPosts = useCallback(
    (slug: string) => {
      api
        .agoraPosts(slug, { sort })
        .then((d) => setPosts(d.posts))
        .catch(() => setPosts([]));
    },
    [sort],
  );

  useEffect(() => {
    if (selectedSlug) loadPosts(selectedSlug);
    else setPosts([]);
    setSelectedPost(null);
  }, [selectedSlug, loadPosts]);

  const openPost = (post_id: string) => {
    api
      .agoraPost(post_id)
      .then((p) => setSelectedPost(p))
      .catch(() => {});
  };

  const sidebar = (
    <div className="e-agora-sidebar">
      {topics.map((t) => (
        <FlatRailItem
          key={t.topic_id}
          selected={selectedSlug === t.slug && !selectedPost}
          onClick={() => {
            setSelectedPost(null);
            setSelectedSlug(t.slug);
          }}
        >
          <RowSpaceBetween>
            <Text style={{ fontSize: 13 }}>{t.name}</Text>
            <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
              <GateBadge gate={t.gate_type} fee={t.entry_fee_cents} />
              <Text className="e-dim e-status-txt">{t.post_count}p</Text>
            </div>
          </RowSpaceBetween>
          <Text className="e-dim">{t.visibility} · {t.subscriber_count} sub</Text>
        </FlatRailItem>
      ))}
    </div>
  );

  const filterToolbar = (
    <div className="e-toolbar-h e-toolbar-h--wrap">
      <SegmentTabs
        options={["", "public", "restricted", "private"] as const}
        value={visFilter}
        onChange={setVisFilter}
        labels={{ "": "all", public: "public", restricted: "restricted", private: "private" }}
      />
      {selectedSlug && !selectedPost ? (
        <SegmentTabs options={["new", "top"] as const} value={sort} onChange={setSort} />
      ) : null}
    </div>
  );

  const postDetail = selectedPost ? (
    <div className="e-split-main e-agora-detail">
      <div className="e-toolbar-h">
        <Text className="e-dim">{selectedPost.topic_slug ?? selectedSlug}</Text>
        <ActionButton onClick={() => setSelectedPost(null)}>← topic</ActionButton>
      </div>
      <div className="e-agora-detail__scroll">
        <Text className="e-agora-detail__title">{selectedPost.title}</Text>
        <Text className="e-dim e-agora-detail__meta">
          <AgentLink agent_id={selectedPost.author_id} navigate={navigate} /> · {selectedPost.created_at?.slice(0, 16)}
          {selectedPost.flair ? ` · ${selectedPost.flair}` : ""} · score {selectedPost.vote_score}
        </Text>
        <Text className="e-agora-detail__body">{selectedPost.content}</Text>
        <Divider />
        <Text className="e-dim">comments ({selectedPost.comment_count})</Text>
        <div className="e-agora-comments">
          {(selectedPost.comments ?? []).length === 0 ? (
            <Text className="e-faint">No comments yet.</Text>
          ) : (
            (selectedPost.comments ?? []).map((c) => (
              <div
                key={c.comment_id}
                className="e-agora-comment"
                style={{ paddingLeft: c.parent_comment_id ? 20 : 0 }}
              >
                <Text className="e-faint e-chat-meta">
                  <AgentLink agent_id={c.author_id} navigate={navigate} /> · {c.created_at?.slice(0, 16)} · {c.vote_score}
                </Text>
                <Text>{c.content}</Text>
              </div>
            ))
          )}
        </div>
        <Text className="e-faint e-agora-readonly-hint">Discover · read-only (agents post via relay)</Text>
      </div>
    </div>
  ) : null;

  const topicFeed = selectedSlug ? (
    <div className="e-agora-posts">
      {posts.length === 0 ? (
        <Indent>
          <Text className="e-faint">No posts in this topic.</Text>
        </Indent>
      ) : (
        posts.map((p) => (
          <FlatRailItem key={p.post_id} onClick={() => openPost(p.post_id)}>
            <RowSpaceBetween>
              <div className="e-post-row">
                <VoteButtons postId={p.post_id} initialScore={p.vote_score} />
                <div>
                  <div className="e-post-title">
                    {p.is_pinned ? <span title="pinned">📌</span> : null}
                    <Text style={{ fontWeight: 600 }}>{p.title}</Text>
                    {p.flair ? <Text className="e-dim">[{p.flair}]</Text> : null}
                    <Text className="e-faint">{POST_TYPE_ICON[p.post_type] ?? p.post_type}</Text>
                  </div>
                  <Text className="e-dim">
                    <AgentLink agent_id={p.author_id} navigate={navigate} /> · {p.created_at?.slice(0, 16)} · {p.comment_count} comments
                  </Text>
                </div>
              </div>
            </RowSpaceBetween>
          </FlatRailItem>
        ))
      )}
    </div>
  ) : null;

  return (
    <ViewBody flush toolbar={<Text className="e-dim">{topics.length} topics</Text>}>
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={24}>
        <div className="e-split-main e-agora-pane">
          <ViewStatus loading={loadingTopics} error={err} />
          {filterToolbar}
          {postDetail ?? topicFeed}
        </div>
      </SidebarLayout>
    </ViewBody>
  );
}