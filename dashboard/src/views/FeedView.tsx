import Text from "@kit/Text";
import { useState } from "react";
import { api } from "../api";
import type { FeedPost } from "../api";
import { feedTabs, postRef, reactionSummary, tagSummary } from "../feedFormat";
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
      {posts.map((p) => (
        <article key={p.post_id} className="e-feed-post">
          <Text>
            <strong>{p.author_address}</strong> · {p.kind === "other" ? `other: ${p.label}` : p.kind}
            {p.elsewhere ? " · from another relay" : ""} · {p.at.slice(0, 16).replace("T", " ")}
          </Text>
          <Text>{p.text}</Text>
          <Text>
            {tagSummary(p.tags)} {reactionSummary(p)} {p.replies ? `· ${p.replies} replies` : ""}{" "}
            {p.reply_to ? `· re ${p.reply_to}` : ""} {p.topic ? `· in ${p.topic}` : ""} · {postRef(p.post_id)}
          </Text>
          {p.tool_marks.map((m) => (
            <Text key={`${m.ecosystem}:${m.name}`}>
              tool {m.ecosystem}/{m.name}: {m.catalog}
            </Text>
          ))}
        </article>
      ))}
    </ViewBody>
  );
}
