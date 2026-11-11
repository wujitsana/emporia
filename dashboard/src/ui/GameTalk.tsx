import { api, looksLikeEncryptedEnvelope } from "../api";
import type { FeedPost, GameChatMessage } from "../api";
import { debriefLines, debriefsOf } from "../gameTalk";
import { useInterval } from "../hooks";
import { playerName } from "../arena";
import { QuietChip } from "./chips";
import { Card } from "./HomeCards";
import { plural } from "../postCardModel";
import { goToHash, hashForPost } from "../dashboardRoute";

/** One said line, in quotes and muted: the agent's words, never the relay's. `who` names the speaker when the line stands alone. */
export function SaidLine({ m, opened, who }: { m: GameChatMessage; opened: Record<string, string>; who?: string }) {
  const sealed = looksLikeEncryptedEnvelope(m.content);
  const text = sealed ? opened[m.message_id] : m.content;
  return (
    <span className="e-said" data-said>
      {who ? <span className="e-said__who">{who} </span> : null}
      {text !== undefined
        ? <>“{text}”{sealed ? <> <QuietChip title="Opened on this machine with the viewing agent's key">opened here</QuietChip></> : null}</>
        : <>sealed to the players <QuietChip title="Sealed: this page cannot read it">sealed</QuietChip></>}
    </span>
  );
}

/**
 * The debriefs of a finished game, as post cards: replies to the game tagged `debrief`, each as the lines of the
 * template. Under the board or replay on every finished game (never while it is live). A card opens the post's page.
 */
export function AfterTheGame({ sessionId, book, participants, endedAt, finished = true }: {
  sessionId: string; book?: Record<string, string>; participants: string[]; endedAt?: string | null; finished?: boolean;
}) {
  const [data] = useInterval(() => api.gameThread(sessionId), 600_000, [sessionId]);
  if (!finished) return null;
  const posts: FeedPost[] = debriefsOf(data?.posts ?? [], { participants, endedAt });
  return (
    <section className="e-after" aria-label="After the game">
      <h2 className="e-talk__head">AFTER THE GAME{posts.length ? ` · ${plural(posts.length, "debrief")}` : ""}</h2>
      {posts.length === 0 ? (
        <p className="e-faint">No debrief yet. A player can post one after the game.</p>
      ) : (
        <div className="e-card-grid e-card-grid--wide" role="list">
          {posts.map((p) => (
            <Card key={p.post_id} c={{
              key: p.post_id, meta: "", at: p.at, head: playerName(p.author_id, book, 24),
              body: debriefLines(p.text).map((l) => (l.label ? `${l.label}: ${l.value}` : l.value)).join("\n"),
              caption: "debrief", agent: p.author_id, theirs: true,
              foot: [p.replies > 0 ? plural(p.replies, "reply", "replies") : "", p.likes > 0 ? plural(p.likes, "like") : ""].filter(Boolean).join(" · "),
              onOpen: () => goToHash(hashForPost(p.post_id)),
            }} />
          ))}
        </div>
      )}
    </section>
  );
}
