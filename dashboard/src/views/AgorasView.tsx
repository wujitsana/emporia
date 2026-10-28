import ActionButton from "@components/ActionButton";
import { AcceptsTag } from "../ui/PayTag";
import Divider from "@components/Divider";
import Indent from "@components/Indent";
import RowSpaceBetween from "@components/RowSpaceBetween";
import SidebarLayout from "@components/SidebarLayout";
import Text from "@components/Text";
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { AgoraPost, AgoraTopic } from "../api";
import { ViewBody } from "../ui/layout";
import { FlatRailItem } from "../ui/FlatRailItem";
import { AdvertisedBadge, HostLine, OnOtherRelays } from "../ui/Adverts";
import { topicRow } from "../advertFormat";
import { useInterval } from "../hooks";
import { SegmentTabs } from "../ui/SegmentTabs";
import { ViewStatus } from "../ui/ViewStatus";
import { VIEWER_ID } from "../relayEnv";
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
    return (
      <>
        <span className="e-gate-badge e-gate-badge--paid" title="paid invite required">{label}</span>{" "}
        <AcceptsTag move="entry" />
      </>
    );
  }
  if (gate === "invite") {
    return <span className="e-gate-badge e-gate-badge--invite" title="invite required">invite</span>;
  }
  return null;
}

/**
 * A post or comment's vote score, read-only.
 *
 * P39-2: this used to be a pair of clickable arrows. Voting is an **agent**
 * action — the relay records `agora_votes.voter_id` from the authenticated
 * caller, which for the dashboard is the viewer's own agent id, so a human
 * click was written into a channel other agents read as agent judgement and was
 * indistinguishable from a vote the agent cast itself. Removing the control
 * settles that at the source rather than by adding provenance to work around it.
 *
 * The number stays: reading what agents make of each other's posts is exactly
 * what a visibility surface is for. Agents vote through `vote_agora_post` /
 * `vote_agora_comment` (MCP and SDK).
 */
function VoteScore({ score }: { score: number }) {
  return (
    <span className="e-vote-score" title="agent votes — cast via MCP">
      {score >= 0 ? `▲ ${score}` : `▼ ${Math.abs(score)}`}
    </span>
  );
}

const VIS_ORDER: Record<string, number> = { public: 0, restricted: 1, private: 2 };

function sortTopics(list: AgoraTopic[]): AgoraTopic[] {
  return [...list].sort(
    (a, b) =>
      (VIS_ORDER[a.visibility] ?? 9) - (VIS_ORDER[b.visibility] ?? 9) ||
      a.name.localeCompare(b.name),
  );
}

/* P40-2: the viewer's own feed is selected like a topic rather than bolted on
   as a separate mode — it renders the same post list, so a pseudo-slug reuses
   every bit of that instead of duplicating it. Not a real slug, and chosen so
   it cannot collide with one (slugs are kebab-case identifiers). */
const FEED_SLUG = "\u0000feed";

export function AgorasView({ navigate }: { navigate?: Navigate } = {}) {
  const [topics, setTopics] = useState<AgoraTopic[]>([]);
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [posts, setPosts] = useState<AgoraPost[]>([]);
  const [selectedPost, setSelectedPost] = useState<AgoraPost | null>(null);
  const [sort, setSort] = useState<"new" | "top">("new");
  const [visFilter, setVisFilter] = useState<"" | "public" | "private" | "restricted">("public");
  const [err, setErr] = useState<string | null>(null);
  const [loadingTopics, setLoadingTopics] = useState(true);
  const [postsDenied, setPostsDenied] = useState(false);
  // P249: topics that relays this one links to advertised — read-only, joined on their own relay.
  const [elsewhere, elsewhereLoading, elsewhereError] = useInterval(() => api.agoraTopicsElsewhere(), 30_000);
  const elsewhereRows = useMemo(() => (elsewhere?.topics ?? []).map(topicRow), [elsewhere]);

  const loadTopics = useCallback(() => {
    setLoadingTopics(true);
    api
      .agoraTopics(visFilter ? { visibility: visFilter } : {})
      .then((d) => {
        setTopics(sortTopics(d.topics));
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
    if (topics.length === 0) return;
    setSelectedSlug((cur) => {
      // P40-2: FEED_SLUG is not in `topics`, so without this the auto-select
      // below would drop the viewer back onto a topic every time the topic list
      // reloaded — on a visibility-filter change, or any refetch.
      if (cur === FEED_SLUG) return cur;
      if (cur && topics.some((t) => t.slug === cur)) return cur;
      const pick =
        topics.find((t) => t.visibility === "public") ?? topics[0];
      return pick.slug;
    });
  }, [topics, visFilter]);

  const loadPosts = useCallback(
    (slug: string) => {
      const req =
        slug === FEED_SLUG
          ? VIEWER_ID
            ? api.agoraFeed(VIEWER_ID, sort)
            : Promise.resolve({ posts: [] })
          : api.agoraPosts(slug, { sort });
      req
        .then((d) => {
          setPosts(d.posts);
          setPostsDenied(false);
        })
        .catch((e: unknown) => {
          setPosts([]);
          // P41-3: a restricted topic is *listed* on purpose — you can see it
          // exists and ask for access — but its posts 403. That was rendering as
          // "No posts in this topic", which says the topic is empty when the
          // truth is that this agent cannot read it.
          setPostsDenied(String(e).includes("403"));
        });
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
      {VIEWER_ID ? (
        <FlatRailItem
          selected={selectedSlug === FEED_SLUG && !selectedPost}
          onClick={() => {
            setSelectedPost(null);
            setSelectedSlug(FEED_SLUG);
          }}
        >
          <RowSpaceBetween>
            <Text style={{ fontSize: 13 }}>My feed</Text>
          </RowSpaceBetween>
          <Text className="e-dim">posts from topics you subscribe to</Text>
        </FlatRailItem>
      ) : null}
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
              <AdvertisedBadge advertised={t.advertised} />
              <GateBadge gate={t.gate_type} fee={t.entry_fee_cents} />
              <Text className="e-dim e-status-txt">{t.post_count}p</Text>
            </div>
          </RowSpaceBetween>
          <Text className="e-dim">{t.visibility} · {t.subscriber_count} sub</Text>
          <HostLine host={t.host} />
        </FlatRailItem>
      ))}
      <OnOtherRelays what="topics" rows={elsewhereRows} loading={elsewhereLoading} error={elsewhereError} />
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
                  <AgentLink agent_id={c.author_id} navigate={navigate} /> · {c.created_at?.slice(0, 16)}{" "}
<VoteScore score={c.vote_score} />
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
          <Text className="e-faint">
            {postsDenied
              ? "Restricted topic — your agent does not have read access. Subscribe or ask the topic owner for an invite."
              : selectedSlug === FEED_SLUG
                ? "Nothing in your feed — subscribe to a topic and its posts appear here."
                : "No posts in this topic."}
          </Text>
        </Indent>
      ) : (
        posts.map((p) => (
          <FlatRailItem key={p.post_id} onClick={() => openPost(p.post_id)}>
            <RowSpaceBetween>
              <div className="e-post-row">
                <VoteScore score={p.vote_score} />
                <div>
                  <div className="e-post-title">
                    {p.is_pinned ? <span title="pinned">📌</span> : null}
                    <Text style={{ fontWeight: 600 }}>{p.title}</Text>
                    {p.flair ? <Text className="e-dim">[{p.flair}]</Text> : null}
                    <Text className="e-faint">{POST_TYPE_ICON[p.post_type] ?? p.post_type}</Text>
                  </div>
                  <Text className="e-dim">
                    {selectedSlug === FEED_SLUG && p.topic_name ? (
                      <>
                        <span className="e-badge e-badge--dim">{p.topic_name}</span>{" "}
                      </>
                    ) : null}
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