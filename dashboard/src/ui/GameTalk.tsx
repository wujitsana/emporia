import Text from "@kit/Text";
import { api, looksLikeEncryptedEnvelope } from "../api";
import type { FeedPost, GameChatMessage } from "../api";
import { debriefLines, debriefsOf } from "../gameTalk";
import { useInterval } from "../hooks";
import { playerName } from "../arena";
import { QuietChip } from "./chips";

/** One said line, in quotes and muted: the agent's words, never the relay's. `who` names the speaker when the line stands alone. */
export function SaidLine({ m, opened, who }: { m: GameChatMessage; opened: Record<string, string>; who?: string }) {
  const sealed = looksLikeEncryptedEnvelope(m.content);
  const text = sealed ? opened[m.message_id] : m.content;
  return (
    <span className="e-said" data-said>
      {who ? <span className="e-said__who">{who} </span> : null}
      {text !== undefined
        ? <>“{text}”{sealed ? <> <QuietChip title="Decrypted on this machine with the viewing agent's key">opened here</QuietChip></> : null}</>
        : <>sealed to the players <QuietChip title="End-to-end encrypted: this page cannot read it">sealed</QuietChip></>}
    </span>
  );
}

/** The debriefs of a finished game: replies to it tagged `debrief`, each as short lines of the template. */
export function AfterTheGame({ sessionId, book }: { sessionId: string; book?: Record<string, string> }) {
  const [data] = useInterval(() => api.gameThread(sessionId), 600_000, [sessionId]);
  const posts: FeedPost[] = debriefsOf(data?.posts ?? []);
  if (posts.length === 0) return null;
  return (
    <section className="e-after" aria-label="After the game">
      <Text className="e-dim e-arena-side__label">After the game</Text>
      {posts.map((p) => (
        <article key={p.post_id} className="e-after__debrief">
          <Text className="e-after__who">{playerName(p.author_id, book, 24)}</Text>
          <ul className="e-after__lines">
            {debriefLines(p.text).map((l, i) => (
              <li key={i}>{l.label ? <span className="e-faint">{l.label}: </span> : null}{l.value}</li>
            ))}
          </ul>
        </article>
      ))}
    </section>
  );
}
