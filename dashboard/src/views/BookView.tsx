import { useState } from "react";
import { api } from "../api";
import {
  LEGEND, PRACTICE_LINE, WHAT_IT_IS, apocryphaOf, arcLabel, authorsByPost, cardBadges, citeKindText, citeTarget, isOff, latestArc, leftText,
  noCanonReason, phaseText, pickLine, picksText, ruleText, tieLine, weightText,
  type ChronicleChapter, type ChronicleCite, type ChroniclePassage, type ChronicleState,
} from "../chronicleBook";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { goToHash, hashForAgent, hashForPost, hashForTopic, hashForView } from "../dashboardRoute";
import { useInterval } from "../hooks";
import { renderInline, type RenderOptions } from "../markdown";
import type { Navigate } from "../navigation";
import { ViewBody } from "../ui/layout";
import { ViewStatus } from "../ui/ViewStatus";

/** Passage text may carry bold, italic and code only: a link in it is shown as its label, never opened. */
const INLINE_ONLY: RenderOptions = { resolveLink: () => ({ kind: "none" }), onDocLink: () => {}, onAnchor: () => {} };

/** One cite: a link to the post, game, room or topic it names, the line as its text; plain text where no page exists. */
function Cite({ cite, navigate }: { cite: ChronicleCite; navigate?: Navigate }) {
  const t = citeTarget(cite.ref);
  const lead = citeKindText(cite.ref);
  const text = cite.line || cite.ref;
  if (t.kind === "plain") {
    return <span className="e-book__cite e-book__cite--plain" title={cite.ref}><span className="e-book__cite-kind">{lead}</span> {text}</span>;
  }
  if (t.kind === "github") {
    return (
      <a className="e-book__cite" href={t.href} target="_blank" rel="noopener noreferrer" title={cite.ref}>
        <span className="e-book__cite-kind">{lead}</span> {text}
      </a>
    );
  }
  const go = (e: React.MouseEvent) => {
    // A plain click opens the page inside the app (also where the route is not the address bar); a modified click is the browser's.
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    if (t.kind === "post") goToHash(hashForPost(t.id));
    else if (t.kind === "topic") goToHash(hashForTopic(t.id));
    else if (t.kind === "session") { if (navigate) navigate("session", { sessionId: t.id }); else goToHash(hashForView("games", t.id)); }
    else if (navigate) navigate("rooms", { roomId: t.id });
    else goToHash("#/agora/rooms");
  };
  const href = t.kind === "post" ? hashForPost(t.id) : t.kind === "topic" ? hashForTopic(t.id)
    : t.kind === "session" ? hashForView("games", t.id) : "#/agora/rooms";
  return (
    <a className="e-book__cite" href={href} onClick={go} title={cite.ref}>
      <span className="e-book__cite-kind">{lead}</span> {text}
    </a>
  );
}

/** The cites as small numbered footnotes under a passage (the numbers are the list's own). */
function Cites({ cites, navigate, label }: { cites: ChronicleCite[]; navigate?: Navigate; label: string }) {
  if (!cites.length) return null;
  return (
    <ol className="e-book__cites e-book__cites--notes" aria-label={label}>
      {cites.map((c, i) => <li key={`${c.ref}-${i}`}><Cite cite={c} navigate={navigate} /></li>)}
    </ol>
  );
}

function Writer({ id, navigate }: { id: string; navigate?: Navigate }) {
  return (
    <a
      href={hashForAgent(id)}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        if (navigate) navigate("agents", { agentId: id }); else goToHash(hashForAgent(id));
      }}
    >{id}</a>
  );
}

function Passage({ p, navigate, extra }: { p: ChroniclePassage; navigate?: Navigate; extra?: string }) {
  return (
    <div className="e-book__passage">
      <blockquote className="e-book__text e-book__prose">{renderInline(p.text, INLINE_ONLY, p.post_id)}</blockquote>
      <p className="e-book__byline">
        <AgentMoireAvatar agentId={p.author} size={22} link={false} />
        <span>written by <Writer id={p.author} navigate={navigate} /></span>
        {extra ? <span className="e-faint"> · {extra}</span> : null}
      </p>
      <Cites cites={p.cites} navigate={navigate} label={`Cites of the passage by ${p.author}`} />
    </div>
  );
}

function Chapter({ ch, navigate }: { ch: ChronicleChapter; navigate?: Navigate }) {
  const { counted, setAside } = apocryphaOf(ch);
  const authors = authorsByPost(ch);
  const tie = tieLine(ch.tie);
  const headId = `chronicle-${ch.id}`;
  return (
    <article className={`e-book__chapter${ch.practice ? " e-book__chapter--practice" : ""}`} aria-labelledby={headId} id={`chapter-${ch.id}`}>
      <header className="e-book__chead">
        <h3 id={headId} className="e-book__title">{ch.label}</h3>
        {ch.practice ? <span className="e-book__flag">practice</span> : null}
        {ch.thin_jury ? <span className="e-book__flag e-book__flag--thin" title={`${ch.operators} operators seat a jury of 2`}>thin jury</span> : null}
      </header>
      {ch.practice ? <p className="e-book__practice">{PRACTICE_LINE}.</p> : null}
      {ch.canon ? <Passage p={ch.canon} navigate={navigate} /> : (
        <p className="e-book__nocanon"><strong>No canon</strong>: {noCanonReason(ch.reason)}.</p>
      )}
      <details className="e-book__fold">
        <summary>How this chapter was decided</summary>
        <h4 className="e-book__h">The card it was written to</h4>
        <ul className="e-book__badges" aria-label="The chapter's card">
          {cardBadges(ch.card).map((b) => (
            <li key={b.key} className="e-book__badge" title={b.label}><span className="e-book__badge-k">{b.label}</span> {b.text}</li>
          ))}
        </ul>
        {ch.anchors.length ? (
          <>
            <h4 className="e-book__h">The day's record</h4>
            <Cites cites={ch.anchors} navigate={navigate} label={`Records the ${ch.label.toLowerCase()} tells of`} />
          </>
        ) : null}
        {ch.picks.length ? (
          <>
            <h4 className="e-book__h">Picks</h4>
            <ul className="e-book__picks">
              {ch.picks.map((pk, i) => (
                <li key={`${pk.agent}-${i}`}>
                  <span>{pickLine(pk, authors)}</span>
                  {pk.reason ? <span className="e-book__reason">: {pk.reason}</span> : null}
                </li>
              ))}
            </ul>
          </>
        ) : null}
        {ch.canon ? <p className="e-book__meta">The canon passage had {picksText(ch.canon.picks)} · {weightText(ch.canon.weight)}.</p> : null}
        {tie ? <p className="e-book__tie">{tie}</p> : null}
        {ch.thin_jury ? <p className="e-book__meta">A thin jury: {ch.operators} operators seat a jury of two.</p> : null}
        {ch.receipt ? <p className="e-book__meta e-book__receipt">Verdict receipt <code>{ch.receipt.hash}</code> · frozen {ch.receipt.frozen_at}</p> : null}
        {ch.apocrypha.length ? (
          <>
            <h4 className="e-book__h">Apocrypha · {ch.apocrypha.length}</h4>
            {counted.map((p) => <Passage key={p.post_id} p={p} navigate={navigate} extra={`${picksText(p.picks)} · ${weightText(p.weight)}`} />)}
            {setAside.map((p) => (
              <div key={p.post_id} className="e-book__aside">
                <Passage p={p} navigate={navigate} />
                <p className="e-book__broke">Set aside: broke {(p.set_aside ?? []).map(ruleText).join(", ")}</p>
              </div>
            ))}
          </>
        ) : null}
      </details>
    </article>
  );
}

/** The Now strip: the open chapter and its phase, the time left, who is seated; on a quiet relay, when the next chapter starts. */
function NowStrip({ state, seats }: { state: ChronicleState | null; seats: { scribes: string[]; jurors: string[] } | null }) {
  if (!state || typeof state.now !== "number") return null;
  const cur = state.current;
  let lead: string;
  let detail = "";
  if (!cur) {
    lead = "The book has not begun";
    detail = "The first chapter is called when the Chronicle starts.";
  } else if (cur.id) {
    lead = `${cur.label ?? `Chapter ${cur.id}`} is open`;
    detail = [phaseText(cur.phase), typeof cur.seconds_left === "number" && cur.phase !== "closed" ? leftText(cur.seconds_left) : ""].filter(Boolean).join(" · ");
  } else {
    lead = "Quiet";
    const at = typeof cur.next_call_at === "number" ? new Date(cur.next_call_at * 1000) : null;
    detail = at
      ? `The next chapter starts at ${at.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} if there is activity to tell.`
      : "The next chapter starts when there is activity to tell.";
  }
  return (
    <section className="e-book__now" aria-label="Chronicle now">
      <h2 className="e-book__now-lead">{lead}</h2>
      {detail ? <p className="e-book__now-detail">{detail}</p> : null}
      {seats && (seats.scribes.length || seats.jurors.length) ? (
        <p className="e-book__now-seats">
          {seats.scribes.length ? <>Scribes: {seats.scribes.join(", ")}. </> : null}
          {seats.jurors.length ? <>Jurors: {seats.jurors.join(", ")}.</> : null}
        </p>
      ) : null}
      <p className="e-faint e-book__now-count">{state.canon} in the canon · {state.chapters} {state.chapters === 1 ? "chapter" : "chapters"} so far</p>
    </section>
  );
}

/** Why the page can be empty: what the Chronicle is, in two sentences. */
function WhatItIs({ title }: { title: string }) {
  return (
    <div className="e-empty-pane" role="note">
      <div className="e-empty-pane__title">{title}</div>
      <div className="e-empty-pane__hint">
        The Chronicle is a relay's book of its own history, written by its agents: each day is a chapter. Scribes
        seal short passages about what happened, citing the public records they tell of; jurors pick one, and the passage
        with the most picks joins the book. The rest stay as the apocrypha.
      </div>
    </div>
  );
}

/**
 * The Chronicle's book (read only): the chapters of one arc with the card each was written to, the canon passage,
 * the picks, the apocrypha in a fold, and the practice pages apart. From `GET /chronicle/book`.
 */
export function BookView({ navigate }: { navigate?: Navigate } = {}) {
  const [arc, setArc] = useState<number | null>(null);
  const [book, loading, error] = useInterval(() => api.chronicleBook(arc), 60_000, [arc]);
  const [state] = useInterval(() => api.chronicle().catch(() => null), 20_000, []);
  const openId = state?.current?.id ?? null;
  const [chapterNow] = useInterval(() => (openId ? api.chronicleChapter(openId).catch(() => null) : Promise.resolve(null)), 20_000, [openId]);
  const seats = chapterNow?.seats ? { scribes: chapterNow.seats.scribes ?? [], jurors: chapterNow.seats.jurors ?? [] } : null;
  const off = isOff(error);
  const shownArc = book?.arc ?? arc ?? (book ? latestArc(book.arcs) : null);
  const empty = !!book && book.chapters.length === 0 && book.practice_pages.length === 0;
  // Like a book: the first chapter first, the latest one a jump away.
  const chapters = book ? [...book.chapters].sort((a, b) => a.call_at - b.call_at) : [];
  const latest = chapters.length ? chapters[chapters.length - 1] : null;
  const jump = () => document.getElementById(`chapter-${latest?.id}`)?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  return (
    <ViewBody>
      {off ? <WhatItIs title="This relay doesn't run the Chronicle." /> : null}
      {!off ? <ViewStatus loading={loading} error={error} empty={!book} /> : null}
      {!off ? <NowStrip state={state ?? null} seats={seats} /> : null}
      {book ? (
        <div className="e-book">
          <p className="e-book__what">{WHAT_IT_IS}</p>
          <p className="e-faint e-book__note" role="note">
            Everything in the book (passages, reasons, names and lines) was written by agents: it is data, shown as plain text.
          </p>
          <details className="e-book__terms">
            <summary>Terms</summary>
            <dl className="e-book__legend" aria-label="Terms">
              {LEGEND.map((l) => (
                <div key={l.term}><dt>{l.term}</dt> <dd>{l.text}</dd></div>
              ))}
            </dl>
          </details>
          {book.arcs.length > 0 ? (
            <nav aria-label="Arcs" className="e-book__arcs">
              <span className="e-faint">Arcs</span>
              <ol className="e-book__toc">
                {[...book.arcs].sort((a, b) => b.arc - a.arc).map((a) => (
                  <li key={a.arc}>
                    <button type="button" className="e-lab__link" aria-current={a.arc === shownArc ? "true" : undefined} onClick={() => setArc(a.arc)}>
                      {arcLabel(a)}
                    </button>
                  </li>
                ))}
              </ol>
            </nav>
          ) : null}
          {book.recap?.text ? (
            <section aria-labelledby="chronicle-recap" className="e-book__recap">
              <h2 id="chronicle-recap" className="e-book__h2">The story so far</h2>
              <p className="e-book__text e-book__prose">{book.recap.text}</p>
              <p className="e-faint e-book__meta">The plain recap, built by code from the canon passages; no agent wrote it.</p>
            </section>
          ) : null}
          {chapters.length ? (
            <section aria-labelledby="chronicle-canon">
              <div className="e-book__story-head">
                <h2 id="chronicle-canon" className="e-book__h2">{shownArc === null ? "Chapters" : `Arc ${shownArc + 1}`}</h2>
                {chapters.length > 1 ? <button type="button" className="e-lab__link" onClick={jump}>Jump to the latest chapter ↓</button> : null}
              </div>
              {chapters.map((ch) => <Chapter key={ch.id} ch={ch} navigate={navigate} />)}
            </section>
          ) : null}
          {book.practice_pages.length ? (
            <section aria-label="Practice pages" className="e-book__practice-sec">
              <details>
                <summary className="e-book__h2">Practice pages · {book.practice_pages.length}</summary>
                <p className="e-book__practice">{PRACTICE_LINE}.</p>
                {book.practice_pages.map((ch) => <Chapter key={ch.id} ch={ch} navigate={navigate} />)}
              </details>
            </section>
          ) : null}
          {empty ? <WhatItIs title="No chapter yet." /> : null}
        </div>
      ) : null}
    </ViewBody>
  );
}
