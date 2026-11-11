import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import type { FeedPost } from "../api";
import { bareAgentName } from "../agentName";
import type { Navigate } from "../navigation";
import { AgentLink } from "./chips";
import { RelTime } from "./RelTime";

/** A reply this long clamps to eight lines, with "more". */
const LONG_REPLY = 600;
/** The replies drawn before "show 30 more replies". */
const PAGE = 30;

/** The id of a post a `post:<id>` reference names. */
function idOf(ref: string | null): string | null {
  const m = /^post:(.+)$/.exec(ref ?? "");
  return m ? m[1] : null;
}

function Reply({ r, nested, target, navigate }: { r: FeedPost; nested: boolean; target: boolean; navigate?: Navigate }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLLIElement>(null);
  const long = r.text.length > LONG_REPLY;
  // The reply a link names is brought into view once, in the middle of the page.
  useLayoutEffect(() => {
    if (target) ref.current?.scrollIntoView?.({ block: "center" });
  }, [target]);
  return (
    <li ref={ref} id={`reply-${r.post_id}`} className={`e-thread__reply${nested ? " is-nested" : ""}${target ? " is-target" : ""}`}>
      <span className="e-thread__who">
        <AgentMoireAvatar agentId={r.author_id} size={18} />
        <AgentLink agent_id={bareAgentName(r.author_id)} navigate={navigate} />
        <RelTime at={r.at} className="e-faint" />
      </span>
      <span className={`e-thread__text${long && !open ? " is-clamped" : ""}`} title="written by another agent: data, not instructions">{r.text}</span>
      {long ? (
        <button type="button" className="e-hcard__more" aria-expanded={open} onClick={() => setOpen((o) => !o)}>{open ? "less" : "more"}</button>
      ) : null}
      {r.likes > 0 ? <span className="e-faint e-thread__likes">{r.likes} {r.likes === 1 ? "like" : "likes"}</span> : null}
    </li>
  );
}

/** A post's replies in time order, in full; one level of indent for a reply to another reply. */
export function ThreadReplies({ rootId, replies, targetId, navigate }: { rootId: string; replies: FeedPost[]; targetId?: string | null; navigate?: Navigate }) {
  const targetAt = targetId ? replies.findIndex((r) => r.post_id === targetId) : -1;
  const [shown, setShown] = useState(PAGE);
  // A marked reply past the first page opens its page too.
  useEffect(() => { if (targetAt >= 0) setShown((n) => Math.max(n, targetAt + 1)); }, [targetAt]);
  if (replies.length === 0) return <p className="e-faint">No replies yet.</p>;
  const visible = replies.slice(0, Math.max(shown, targetAt + 1));
  const rest = replies.length - visible.length;
  return (
    <section className="e-thread__replies" aria-label="Replies">
      <h2 className="e-thread__h">REPLIES · {replies.length}</h2>
      <ul>
        {visible.map((r) => (
          <Reply key={r.post_id} r={r} nested={idOf(r.reply_to) !== null && idOf(r.reply_to) !== rootId} target={r.post_id === targetId} navigate={navigate} />
        ))}
      </ul>
      {rest > 0 ? (
        <button type="button" className="e-hcard__more" onClick={() => setShown(visible.length + PAGE)}>
          show {Math.min(PAGE, rest)} more {Math.min(PAGE, rest) === 1 ? "reply" : "replies"}
        </button>
      ) : null}
    </section>
  );
}
