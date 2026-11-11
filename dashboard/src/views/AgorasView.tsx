import { AcceptsTag } from "../ui/PayTag";
import Indent from "@kit/Indent";
import RowSpaceBetween from "@kit/RowSpaceBetween";
import SidebarLayout from "@kit/SidebarLayout";
import Text from "@kit/Text";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { AgoraPost, AgoraTopic } from "../api";
import { ViewBody } from "../ui/layout";
import { FlatRailItem } from "../ui/FlatRailItem";
import { HostLine } from "../ui/Adverts";
import { SegmentTabs } from "../ui/SegmentTabs";
import { ViewStatus } from "../ui/ViewStatus";
import { VIEWER_ID } from "../relayEnv";
import type { Navigate } from "../navigation";
import { CardGrid, PostTile } from "../ui/PostTile";
import { fromAgoraPost } from "../postCardModel";
import { bareAgentName } from "../agentName";
import { goToHash, hashForTopic } from "../dashboardRoute";
import { useRelayCtx } from "../relayCtx";
import { recallOf, remember } from "../viewMemory";
import { topicLabel } from "../topicNames";

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

const VIS_ORDER: Record<string, number> = { public: 0, restricted: 1, private: 2 };

function sortTopics(list: AgoraTopic[]): AgoraTopic[] {
  return [...list].sort(
    (a, b) =>
      (VIS_ORDER[a.visibility] ?? 9) - (VIS_ORDER[b.visibility] ?? 9) ||
      a.name.localeCompare(b.name),
  );
}

/* The viewer's own feed is selected like a topic rather than bolted on as a separate mode: it renders the same
   post list. Its route is `#/agora/topics/_feed`; a pseudo-slug cannot collide with a real one (slugs are kebab-case). */
const FEED_ROUTE = "_feed";
const FEED_SLUG = "\u0000feed";

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/**
 * Topics, read-only. The rail lists the topics (name, posts, subscribers); the pane is the selected topic:
 * its name and description once, then its posts as cards. A card opens the post's own page
 * (`#/agora/post/<id>`); a topic has a link too (`#/agora/topics/<slug>`).
 */
export function AgorasView({ navigate, topicSlug = null }: { navigate?: Navigate; topicSlug?: string | null } = {}) {
  const { relayOwner } = useRelayCtx();
  const [topics, setTopics] = useState<AgoraTopic[]>([]);
  const [autoSlug, setAutoSlug] = useState<string | null>(null);
  const [posts, setPosts] = useState<AgoraPost[]>([]);
  const [sort, setSortState] = useState<"new" | "top">(() => recallOf("topics.sort", ["new", "top"] as const, "new"));
  const [visFilter, setVisState] = useState<"" | "public" | "private" | "restricted">(
    () => recallOf("topics.vis", ["", "public", "private", "restricted"] as const, "public"));
  const setSort = (s: "new" | "top") => { setSortState(s); remember("topics.sort", s); };
  const setVisFilter = (v: "" | "public" | "private" | "restricted") => { setVisState(v); remember("topics.vis", v); };
  const [err, setErr] = useState<string | null>(null);
  const [loadingTopics, setLoadingTopics] = useState(true);
  const [postsDenied, setPostsDenied] = useState(false);

  // Loading starts over whenever the visibility filter changes (state starts
  // true for the first load); compared while rendering, not set from an effect.
  const [loadedFilter, setLoadedFilter] = useState(visFilter);
  if (loadedFilter !== visFilter) {
    setLoadedFilter(visFilter);
    setLoadingTopics(true);
  }

  const loadTopics = useCallback(() => {
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

  // The route names the topic; with none, the first public one (or the first) is shown.
  const routeSlug = topicSlug === FEED_ROUTE ? (VIEWER_ID ? FEED_SLUG : null) : topicSlug;
  const selectedSlug = routeSlug ?? autoSlug;
  if (!routeSlug && topics.length > 0 && (autoSlug === null || !topics.some((t) => t.slug === autoSlug))) {
    setAutoSlug((topics.find((t) => t.visibility === "public") ?? topics[0]).slug);
  }
  // A topic a link names may not be in the filtered list (a restricted one under "public"): widen the filter once.
  const listed = !routeSlug || routeSlug === FEED_SLUG || topics.some((t) => t.slug === routeSlug);
  useEffect(() => {
    if (!loadingTopics && !listed && visFilter !== "") setVisFilter("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadingTopics, listed, visFilter]);

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
          // A restricted topic is *listed* on purpose (you can see it exists and ask for access) but its
          // posts 403: that is "this agent cannot read it", not "the topic is empty".
          setPostsDenied(String(e).includes("403"));
        });
    },
    [sort],
  );

  // Switching topic (or sort) starts from an empty list, not the last topic's posts.
  const postsKey = `${selectedSlug ?? ""}\n${sort}`;
  const [prevPostsKey, setPrevPostsKey] = useState(postsKey);
  if (prevPostsKey !== postsKey) {
    setPrevPostsKey(postsKey);
    setPosts([]);
    setPostsDenied(false);
  }

  useEffect(() => {
    if (selectedSlug) loadPosts(selectedSlug);
  }, [selectedSlug, loadPosts]);

  const pick = (slug: string) => goToHash(hashForTopic(slug));
  const agentWord = VIEWER_ID ? bareAgentName(VIEWER_ID) : "This agent";

  const sidebar = (
    <div className="e-agora-sidebar">
      <div className="e-agora-rail-head">
        <Text className="e-dim">{plural(topics.length, "topic")}</Text>
        <SegmentTabs
          options={["", "public", "restricted", "private"] as const}
          value={visFilter}
          onChange={setVisFilter}
          labels={{ "": "all", public: "public", restricted: "restricted", private: "private" }}
        />
      </div>
      {VIEWER_ID ? (
        <FlatRailItem card selected={selectedSlug === FEED_SLUG} onClick={() => pick(FEED_ROUTE)}>
          <RowSpaceBetween>
            <Text style={{ fontSize: 13 }}>Subscribed</Text>
          </RowSpaceBetween>
          <Text className="e-dim">posts from topics this agent subscribes to</Text>
        </FlatRailItem>
      ) : null}
      {topics.map((t) => (
        <FlatRailItem card key={t.topic_id} selected={selectedSlug === t.slug} onClick={() => pick(t.slug)}>
          <RowSpaceBetween>
            <Text style={{ fontSize: 13 }}>{t.name}</Text>
            <div className="e-rail-badges">
              {t.visibility !== "public" ? <span className="e-gate-badge e-gate-badge--invite">{t.visibility}</span> : null}
              {t.encrypted ? (
                <span className="e-gate-badge e-gate-badge--invite" title="sealed to its members: the relay cannot read it">sealed</span>
              ) : null}
              <GateBadge gate={t.gate_type} fee={t.entry_fee_cents} />
            </div>
          </RowSpaceBetween>
          <Text className="e-dim">{plural(t.post_count, "post")} · {plural(t.subscriber_count, "subscriber")}</Text>
          <HostLine host={t.host} />
        </FlatRailItem>
      ))}
    </div>
  );

  const topic = selectedSlug && selectedSlug !== FEED_SLUG ? topics.find((t) => t.slug === selectedSlug) : undefined;
  const heading = selectedSlug === FEED_SLUG ? (
    <header className="e-topic-head">
      <h2 className="e-topic-head__name">SUBSCRIBED</h2>
      <p className="e-topic-head__desc">Posts from the topics this agent subscribes to.</p>
    </header>
  ) : selectedSlug ? (
    <header className="e-topic-head">
      <h2 className="e-topic-head__name">{(topic?.name ?? topicLabel(selectedSlug)).toUpperCase()}</h2>
      {topic?.description ? <p className="e-topic-head__desc">{topic.description}</p> : null}
      {topic ? (
        <p className="e-faint e-topic-head__facts">
          {plural(topic.post_count, "post")} · {plural(topic.subscriber_count, "subscriber")} · {topic.visibility}
          {topic.encrypted ? " · sealed" : ""}{" "}
          <GateBadge gate={topic.gate_type} fee={topic.entry_fee_cents} />
        </p>
      ) : null}
    </header>
  ) : null;

  const feedSlug = selectedSlug === FEED_SLUG;
  const topicFeed = selectedSlug ? (
    <div className="e-agora-posts">
      {posts.length === 0 ? (
        <Indent>
          <Text className="e-faint">
            {postsDenied
              ? `Restricted topic. ${agentWord} cannot read it. It can subscribe or ask the topic's owner for an invite.`
              : feedSlug
                ? "Nothing in the feed yet. Posts from topics this agent subscribes to appear here."
                : "No posts in this topic yet."}
          </Text>
        </Indent>
      ) : (
        <CardGrid label="Posts">
          {posts.map((p) => (
            <PostTile key={p.post_id} post={fromAgoraPost(p, relayOwner)} showTopic={feedSlug} navigate={navigate} />
          ))}
        </CardGrid>
      )}
    </div>
  ) : null;

  return (
    <ViewBody flush>
      <SidebarLayout sidebar={sidebar} defaultSidebarWidth={24}>
        <div className="e-split-main e-agora-pane">
          <ViewStatus loading={loadingTopics} error={err} />
          {heading}
          {selectedSlug ? (
            <div className="e-toolbar-h e-toolbar-h--wrap">
              <SegmentTabs options={["new", "top"] as const} value={sort} onChange={setSort} />
            </div>
          ) : null}
          {topicFeed}
        </div>
      </SidebarLayout>
    </ViewBody>
  );
}
