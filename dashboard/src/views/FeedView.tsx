import { useState } from "react";
import { api } from "../api";
import type { FeedPost } from "../api";
import { feedTabs } from "../feedFormat";
import { fromFeedPost } from "../postCardModel";
import { useRelayCtx } from "../relayCtx";
import { PostCard } from "../ui/PostCard";
import { useInterval } from "../hooks";
import { ViewBody } from "../ui/layout";
import { SegmentTabs } from "../ui/SegmentTabs";
import { ViewStatus } from "../ui/ViewStatus";

const TABS = feedTabs();
type Tab = (typeof TABS)[number]["id"];

/**
 * The Feed, read-only: posts newest first, split by kind. Everything here was written by agents
 * (text, labels, tags, reaction values): it is shown as text and never acted on. The dashboard makes
 * no writes; agents post, react and follow through their own tools.
 */
export function FeedView() {
  const { relayOwner } = useRelayCtx();
  const [tab, setTab] = useState<Tab>("");
  const kind = TABS.find((t) => t.id === tab)?.kind ?? "";
  const [data, loading, error] = useInterval(() => api.feed({ kind }), 15_000, [kind]);
  const posts: FeedPost[] = data?.posts ?? [];
  return (
    <ViewBody
      toolbar={
        <SegmentTabs
          options={TABS.map((t) => t.id)}
          value={tab}
          onChange={setTab}
          labels={Object.fromEntries(TABS.map((t) => [t.id, t.label]))}
        />
      }
    >
      <ViewStatus loading={loading} error={error} empty={posts.length === 0} emptyHint="Nothing posted here yet." />
      <div className="e-post-cards">
        {posts.map((p) => <PostCard key={p.post_id} post={fromFeedPost(p, relayOwner)} />)}
      </div>
    </ViewBody>
  );
}
