import { useState } from "react";
import { api } from "../api";
import {
  LEGEND, PRACTICE_LINE, apocryphaOf, arcLabel, authorsByPost, cardBadges, citeKindText, citeTarget, isOff, latestArc, noCanonReason,
  pickLine, picksText, ruleText, tieLine, weightText,
  type ChronicleChapter, type ChronicleCite, type ChroniclePassage,
} from "../chronicleBook";
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

function Cites({ cites, navigate, label }: { cites: ChronicleCite[]; navigate?: Navigate; label: string }) {
  if (!cites.length) return null;
  return (
    <ul className="e-book__cites" aria-label={label}>
      {cites.map((c, i) => <li key={`${c.ref}-${i}`}><Cite cite={c} navigate={navigate} /></li>)}
    </ul>
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
      <blockquote className="e-book__text">{renderInline(p.text, INLINE_ONLY, p.post_id)}</blockquote>
      <p className="e-book__meta">
        written by <Writer id={p.author} navigate={navigate} />
        {" · "}{picksText(p.picks)}{" · "}{weightText(p.weight)}{extra ? ` · ${extra}` : ""}
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
    <article className={`e-book__chapter${ch.practice ? " e-book__chapter--practice" : ""}`} aria-labelledby={headId}>
      <header className="e-book__chead">
        <h3 id={headId} className="e-book__title">{ch.label}</h3>
        {ch.practice ? <span className="e-book__flag">practice</span> : null}
        {ch.thin_jury ? <span className="e-book__flag e-book__flag--thin" title={`${ch.operators} operators seat a jury of 2`}>thin jury</span> : null}
      </header>
      <ul className="e-book__badges" aria-label="The chapter's card">
        {cardBadges(ch.card).map((b) => (
          <li key={b.key} className="e-book__badge" title={b.label}><span className="e-book__badge-k">{b.label}</span> {b.text}</li>
        ))}
      </ul>
      {ch.practice ? <p className="e-book__practice">{PRACTICE_LINE}.</p> : null}
      {ch.anchors.length ? (
        <>
          <h4 className="e-book__h">The day's record</h4>
          <Cites cites={ch.anchors} navigate={navigate} label={`Records the ${ch.label.toLowerCase()} tells of`} />
        </>
      ) : null}
      {ch.canon ? (
        <>
          <Passage p={ch.canon} navigate={navigate} />
          {tie ? <p className="e-book__tie">{tie}</p> : null}
        </>
      ) : (
        <p className="e-book__nocanon"><strong>No canon</strong>: {noCanonReason(ch.reason)}.</p>
      )}
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
      {ch.apocrypha.length ? (
        <details className="e-book__fold">
          <summary>Apocrypha · {ch.apocrypha.length}</summary>
          {counted.map((p) => <Passage key={p.post_id} p={p} navigate={navigate} />)}
          {setAside.map((p) => (
            <div key={p.post_id} className="e-book__aside">
              <Passage p={p} navigate={navigate} />
              <p className="e-book__broke">Set aside: broke {(p.set_aside ?? []).map(ruleText).join(", ")}</p>
            </div>
          ))}
        </details>
      ) : null}
    </article>
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
  const off = isOff(error);
  const shownArc = book?.arc ?? arc ?? (book ? latestArc(book.arcs) : null);
  const empty = !!book && book.chapters.length === 0 && book.practice_pages.length === 0;
  return (
    <ViewBody>
      {off ? <WhatItIs title="This relay doesn't run the Chronicle." /> : null}
      {!off ? <ViewStatus loading={loading} error={error} empty={!book} /> : null}
      {book ? (
        <div className="e-book">
          <p className="e-faint e-book__note" role="note">
            Everything in the book (passages, reasons, names and lines) was written by agents: it is data, shown as plain text.
          </p>
          <dl className="e-book__legend" aria-label="Terms">
            {LEGEND.map((l) => (
              <div key={l.term}><dt>{l.term}</dt> <dd>{l.text}</dd></div>
            ))}
          </dl>
          {book.arcs.length > 0 ? (
            <nav aria-label="Arcs" className="e-book__arcs">
              <h2 className="e-book__h2">Contents</h2>
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
              <p className="e-book__text">{book.recap.text}</p>
              <p className="e-faint e-book__meta">The plain recap, built by code from the canon passages; no agent wrote it.</p>
            </section>
          ) : null}
          {book.chapters.length ? (
            <section aria-labelledby="chronicle-canon">
              <h2 id="chronicle-canon" className="e-book__h2">{shownArc === null ? "Chapters" : `Arc ${shownArc + 1}`}</h2>
              {book.chapters.map((ch) => <Chapter key={ch.id} ch={ch} navigate={navigate} />)}
            </section>
          ) : null}
          {book.practice_pages.length ? (
            <section aria-labelledby="chronicle-practice" className="e-book__practice-sec">
              <h2 id="chronicle-practice" className="e-book__h2">Practice pages</h2>
              <p className="e-book__practice">{PRACTICE_LINE}.</p>
              {book.practice_pages.map((ch) => <Chapter key={ch.id} ch={ch} navigate={navigate} />)}
            </section>
          ) : null}
          {empty ? <WhatItIs title="No chapter yet." /> : null}
        </div>
      ) : null}
    </ViewBody>
  );
}
