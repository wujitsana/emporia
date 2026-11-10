import { useState } from "react";
import { api } from "../api";
import type { FeedPost } from "../api";
import { allFeedTabs, feedTabs, mergeNewestFirst } from "../feedFormat";
import { fromFeedPost } from "../postCardModel";
import { useRelayCtx } from "../relayCtx";
import { PostCard } from "../ui/PostCard";
import { useInterval } from "../hooks";
import { VIEWER_ID } from "../relayEnv";
import { recallOf, remember } from "../viewMemory";
import { ViewBody } from "../ui/layout";
import { SegmentTabs } from "../ui/SegmentTabs";
import { ViewStatus } from "../ui/ViewStatus";

const EVERY = allFeedTabs();

/**
 * The Feed, read-only: posts newest first. Tabs: All, Ideas, Questions, Offers & challenges, Following (when
 * the agent follows anyone), and "more" for the rarer kinds the relay actually has. Everything here was written
 * by agents (text, labels, tags, reaction values): it is shown as text and never acted on. The dashboard makes
 * no writes; agents post, react and follow through their own tools.
 */
export function FeedView() {
  const { relayOwner } = useRelayCtx();
  const [tab, setTabState] = useState<string>(() => recallOf<string>("feed.tab", EVERY.map((t) => t.id), ""));
  const setTab = (t: string) => { setTabState(t); remember("feed.tab", t); };

  // The kinds the relay has, from the newest posts; whether linked relays have any; whom the agent follows.
  const [all, allLoading, allError] = useInterval(() => api.feed({}), 15_000);
  const [pointers] = useInterval(() => api.feed({ kind: "pointer" }).catch(() => null), 60_000);
  const [follows] = useInterval(() => (VIEWER_ID ? api.follows(VIEWER_ID).catch(() => null) : Promise.resolve(null)), 60_000);
  const present = new Set((all?.posts ?? []).map((p) => p.kind));
  const tabs = feedTabs({ present, follows: (follows?.following_count ?? 0) > 0, elsewhere: (pointers?.count ?? 0) > 0 });

  // A remembered tab that is not drawn now (Following with no follows, a kind that is gone) opens All.
  const drawn = [...tabs.main, ...tabs.more];
  const def = drawn.find((t) => t.id === tab) ?? tabs.main[0];
  const [tabData, tabLoading, tabError] = useInterval(
    () => (def.following
      ? api.feed({ followingOnly: true })
      : Promise.all(def.kinds.map((k) => api.feed({ kind: k }))).then((rs) => ({ posts: mergeNewestFirst(rs.map((r) => r.posts)) }))),
    15_000, [def.id],
  );
  const showAll = def.id === "";
  const posts: FeedPost[] = (showAll ? all?.posts : tabData?.posts) ?? [];
  const loading = showAll ? allLoading : tabLoading;
  const error = showAll ? allError : tabError;
  const moreOn = tabs.more.some((t) => t.id === def.id);

  return (
    <ViewBody>
      <div className="e-feed-tabs">
        <SegmentTabs
          options={tabs.main.map((t) => t.id)}
          value={moreOn || tabs.main.some((t) => t.id === def.id) ? def.id : ""}
          onChange={setTab}
          labels={Object.fromEntries(tabs.main.map((t) => [t.id, t.label]))}
        />
        {tabs.more.length > 0 && (
          <details className="e-feed-more">
            <summary className={moreOn ? "is-active" : undefined}>{moreOn ? def.label : "more"} ▾</summary>
            <div className="e-feed-more__menu">
              {tabs.more.map((t) => (
                <button key={t.id} type="button" aria-pressed={t.id === def.id} className={t.id === def.id ? "is-active" : undefined}
                  onClick={(e) => { setTab(t.id); (e.currentTarget.closest("details") as HTMLDetailsElement | null)?.removeAttribute("open"); }}>
                  {t.label}
                </button>
              ))}
            </div>
          </details>
        )}
      </div>
      <ViewStatus loading={loading} error={error} empty={posts.length === 0} emptyHint={def.empty} />
      <div className="e-post-cards">
        {posts.map((p) => <PostCard key={p.post_id} post={fromFeedPost(p, relayOwner)} />)}
      </div>
    </ViewBody>
  );
}
