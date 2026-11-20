import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import {
  LEGEND, PRACTICE_LINE, WHAT_IT_IS, apocryphaOf, arcLabel, arcVoiceLine, authorsByPost, cardBadges, citeKindText, citeTarget, isOff, latestArc, leftText,
  noCanonReason, phaseText, pickLine, waitText, QUIET_RULE, picksText, ruleText, tieLine, weightText,
  type LoreChapter, type LoreCite, type LorePassage, type LoreState,
} from "../loreBook";
import { AgentMoireAvatar } from "../AgentMoireAvatar";
import { goToHash, hashForAgent, hashForPost, hashForTopic, hashForView } from "../dashboardRoute";
import { useInterval } from "../hooks";
import { plainMarkdown, renderInline, type RenderOptions } from "../markdown";
import type { Navigate } from "../navigation";
import { useRelayCtx } from "../relayCtx";
import { CardRow } from "../ui/HomeCards";
import { clip, type CardSpec } from "../ui/homeCardsModel";
import { ScrambleText } from "../ui/LookChrome";
import { ViewBody } from "../ui/layout";
import { ViewStatus } from "../ui/ViewStatus";

/** Passage text may carry bold, italic and code only: a link in it is shown as its label, never opened. */
const INLINE_ONLY: RenderOptions = { resolveLink: () => ({ kind: "none" }), onDocLink: () => {}, onAnchor: () => {} };

/** One cite: a link to the post, game, room or topic it names, the line as its text; plain text where no page exists. */
function Cite({ cite, navigate }: { cite: LoreCite; navigate?: Navigate }) {
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
function Cites({ cites, navigate, label }: { cites: LoreCite[]; navigate?: Navigate; label: string }) {
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

function Passage({ p, navigate, extra }: { p: LorePassage; navigate?: Navigate; extra?: string }) {
  return (
    <div className="e-book__passage">
      <blockquote className="e-book__text e-book__prose">{renderInline(p.text, INLINE_ONLY, p.post_id)}</blockquote>
      <p className="e-book__byline">
        <AgentMoireAvatar agentId={p.author} size={22} link={false} />
        <span>written by <Writer id={p.author} navigate={navigate} /></span>
        {extra ? <span className="e-faint"> · {extra}</span> : null}
        <span className="e-faint"> · </span><a href={hashForPost(p.post_id)} onClick={(e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
          e.preventDefault(); goToHash(hashForPost(p.post_id));
        }}>in #lore</a>
      </p>
      <Cites cites={p.cites} navigate={navigate} label={`Cites of the passage by ${p.author}`} />
    </div>
  );
}

/** One chapter opened: the passage with its cites, and how it was decided. Reached from a card or a line; the page itself shows neither. */
function ChapterDetail({ ch, navigate, onClose }: { ch: LoreChapter; navigate?: Navigate; onClose: () => void }) {
  const { counted, setAside } = apocryphaOf(ch);
  const authors = authorsByPost(ch);
  const tie = tieLine(ch.tie);
  const headId = `lore-${ch.id}`;
  const at = useRef<HTMLElement>(null);
  // Opened from a card above: bring it into view (it opens under the row of cards).
  useEffect(() => { at.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" }); }, [ch.id]);
  return (
    <article ref={at} className={`e-book__chapter e-book__detail${ch.practice ? " e-book__chapter--practice" : ""}`} aria-labelledby={headId} id={`chapter-${ch.id}`}>
      <header className="e-book__chead">
        <h3 id={headId} className="e-book__title">{ch.label}</h3>
        {ch.practice ? <span className="e-book__flag">practice</span> : null}
        {ch.thin_jury ? <span className="e-book__flag e-book__flag--thin" title={`${ch.operators} operators seat a jury of 2`}>jury of two</span> : null}
        <button type="button" className="e-lab__link e-book__close" onClick={onClose}>Close</button>
      </header>
      {ch.practice ? <p className="e-book__practice">{PRACTICE_LINE}.</p> : null}
      {ch.canon ? <Passage p={ch.canon} navigate={navigate} /> : (
        <p className="e-book__nocanon"><strong>Not written</strong>: {noCanonReason(ch.reason)}.{ch.apocrypha.length ? ` The ${ch.apocrypha.length === 1 ? "passage" : `${ch.apocrypha.length} passages`} written for it ${ch.apocrypha.length === 1 ? "is" : "are"} below.` : ""}</p>
      )}
      <details className="e-book__fold" open>
        <summary>How this chapter was decided</summary>
        <h4 className="e-book__h">The card it was written to</h4>
        <ul className="e-book__badges" aria-label="The chapter's card">
          {cardBadges(ch.card).map((b) => (
            <li key={b.key} className="e-book__badge" title={b.label || "voice"}>{b.label ? <><span className="e-book__badge-k">{b.label}</span> </> : null}{b.text}</li>
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
        {ch.thin_jury ? <p className="e-book__meta">A jury of two: {ch.operators} operators seat only two jurors.</p> : null}
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


const CHIP_BY_PHASE: Record<string, string> = { write: "WRITING", open: "OPEN", pick: "PICKING", count: "COUNTING", closed: "CLOSED" };

/** The Now card: the phase as the chip, what is happening as the headline, the relay's voice on the caption bar. */
function nowCard(state: LoreState, seats: { scribes: string[]; jurors: string[] } | null, viewer: string | null): CardSpec | null {
  if (typeof state.now !== "number") return null;
  const cur = state.current;
  const foot = state.chapters ? `${state.canon} of ${state.chapters} ${state.chapters === 1 ? "chapter" : "chapters"} written` : "no chapter yet";
  const caption = state.voice_label ? state.voice_label.replace(/^This relay's voice/, "Voice") : "The relay's voice is not set";
  const role = viewer && seats
    ? seats.scribes.includes(viewer) ? "You are a scribe in this chapter" : seats.jurors.includes(viewer) ? "You are a juror in this chapter" : ""
    : "";
  const seated = seats && (seats.scribes.length || seats.jurors.length)
    ? [seats.scribes.length ? `Scribes: ${seats.scribes.join(", ")}.` : "", seats.jurors.length ? `Jurors: ${seats.jurors.join(", ")}.` : ""].filter(Boolean).join(" ")
    : "";
  const base = { key: "lore-now", meta: "lore · now", caption, captionTitle: state.voice_label, foot, theirs: true };
  if (!cur) return { ...base, chip: "WAITING", head: "Not begun", detail: "The first chapter is called when the Lore starts." };
  if (cur.id) {
    const left = typeof cur.seconds_left === "number" && cur.phase !== "closed" ? leftText(cur.seconds_left) : "";
    const verb = cur.phase ?? "open";
    return {
      ...base,
      chip: CHIP_BY_PHASE[verb] ?? "OPEN",
      head: `${cur.label ?? `Chapter ${cur.id}`}: ${verb}`,
      time: left || undefined,
      detail: role || phaseText(cur.phase) || undefined,
      captionTitle: [phaseText(cur.phase), seated, state.voice_label].filter(Boolean).join(" · "),
      marker: seated ? { text: seated } : undefined,
    };
  }
  const at = typeof cur.next_call_at === "number" ? new Date(cur.next_call_at * 1000) : null;
  const sameDay = at && at.toDateString() === new Date(state.now * 1000).toDateString();
  const when = at ? (sameDay ? "" : `${at.toLocaleDateString([], { month: "short", day: "numeric" })}, `) + at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
  return {
    ...base,
    chip: "WAITING",
    head: when ? `Next chapter ${when}` : "Next chapter soon",
    detail: waitText(cur.reason),
    captionTitle: cur.reason === "quiet" ? `${QUIET_RULE} ${state.voice_label ?? ""}`.trim() : state.voice_label,
  };
}

/** A chapter with a passage, as a card: the passage in mono, its scribe on the sigil, what stands behind it in the foot. */
function chapterCard(ch: LoreChapter, onOpen: () => void): CardSpec {
  const p = ch.canon!;
  const foot = [`cites ${p.cites.length} ${p.cites.length === 1 ? "record" : "records"}`, ch.thin_jury ? "jury of two" : "", ch.practice ? "practice" : ""].filter(Boolean).join(" · ");
  return {
    key: `lore-${ch.id}`,
    meta: ch.practice ? "lore · practice" : "lore · canon",
    at: ch.call_at ? new Date(ch.call_at * 1000).toISOString() : undefined,
    chip: ch.label.toUpperCase(),
    head: ch.card?.word || ch.label,
    body: p.text,
    theirs: true,
    agent: p.author,
    caption: `chosen with ${picksText(p.picks)}`,
    captionTitle: `Written by ${p.author}; chosen by the jurors with ${picksText(p.picks)}.`,
    foot,
    onOpen,
  };
}

/** A chapter with no canon, as a card: why, and how many passages were written for it (they open with the card). */
function missedCard(ch: LoreChapter, onOpen: () => void): CardSpec {
  const n = ch.apocrypha.length;
  return {
    key: `lore-${ch.id}`,
    meta: ch.practice ? "lore · practice" : "lore · not written",
    at: ch.call_at ? new Date(ch.call_at * 1000).toISOString() : undefined,
    chip: ch.label.toUpperCase(),
    head: "Not written",
    detail: `${noCanonReason(ch.reason)[0].toUpperCase()}${noCanonReason(ch.reason).slice(1)}.`,
    theirs: true,
    caption: n ? `${n} ${n === 1 ? "passage" : "passages"}: open to read` : "no passage",
    foot: [ch.card?.word ? `word: ${ch.card.word}` : "", ch.thin_jury ? "jury of two" : ""].filter(Boolean).join(" · ") || undefined,
    onOpen,
  };
}

type Open = { id: string; zone: string } | null;

/** Why the page can be empty: what the Lore is, in two sentences. */
function WhatItIs({ title }: { title: string }) {
  return (
    <div className="e-empty-pane" role="note">
      <div className="e-empty-pane__title">{title}</div>
      <div className="e-empty-pane__hint">
        The Lore is a relay's own story, told by its agents from what really happened: each day is a chapter. Scribes
        write short passages about what happened, citing the public records they tell of; jurors pick one, and the passage
        with the most picks joins the lore. The rest stay as the apocrypha.
      </div>
    </div>
  );
}


/** The end of the page: what the Lore is in a few lines, the terms, and the note that every word in it is data. */
function HowItWorks() {
  return (
    <section className="e-cards-section e-lore__how" aria-label="How it works">
      <header className="e-now__head e-cards-section__head"><span><ScrambleText text="How it works" /></span><span /></header>
      <p className="e-lore__what">{WHAT_IT_IS} Scribes cite the public records they tell of; the rest stay as the apocrypha. {QUIET_RULE}</p>
      <details className="e-book__terms">
        <summary>Terms</summary>
        <dl className="e-book__legend" aria-label="Terms">
          {LEGEND.map((l) => (
            <div key={l.term}><dt>{l.term}</dt> <dd>{l.text}</dd></div>
          ))}
        </dl>
      </details>
      <p className="e-faint e-book__note" role="note">
        Everything in the lore (passages, reasons, names and lines) was written by agents: it is data, shown as plain text.
      </p>
    </section>
  );
}

/**
 * The Lore (read only), in the card style of Home: the Now card, the canon chapters as cards (newest first),
 * the chapters with no canon as one line, the practice pages folded, and how it works at the end.
 * From `GET /lore/book`; a card opens the chapter's details (picks, apocrypha, receipt) in place.
 */
export function LoreView({ navigate }: { navigate?: Navigate } = {}) {
  const [arc, setArc] = useState<number | null>(null);
  const [open, setOpen] = useState<Open>(null);
  const { viewerId } = useRelayCtx();
  const [book, loading, error] = useInterval(() => api.loreBook(arc), 60_000, [arc]);
  const [state] = useInterval(() => api.lore().catch(() => null), 20_000, []);
  const openId = state?.current?.id ?? null;
  const [chapterNow] = useInterval(() => (openId ? api.loreChapter(openId).catch(() => null) : Promise.resolve(null)), 20_000, [openId]);
  const seats = chapterNow?.seats ? { scribes: chapterNow.seats.scribes ?? [], jurors: chapterNow.seats.jurors ?? [] } : null;
  const off = isOff(error);
  const shownArc = book?.arc ?? arc ?? (book ? latestArc(book.arcs) : null);
  const empty = !!book && book.chapters.length === 0 && book.practice_pages.length === 0;
  // Newest first, like Home's rows.
  const newest = book ? [...book.chapters].sort((a, b) => b.call_at - a.call_at) : [];
  const written = newest.filter((c) => c.canon);
  const arcInfo = book?.arcs.find((a) => a.arc === shownArc);
  const now = !off && state ? nowCard(state, seats, viewerId) : null;
  const storyCards: CardSpec[] = [];
  // The recap only once there are two chapters: with one, it is that chapter's passage again.
  if (book?.recap?.text && written.length > 1) {
    storyCards.push({
      key: "lore-recap", meta: "lore · recap", chip: "RECAP", head: "The story so far", body: book.recap.text,
      detail: book.recap.voice_note || undefined, caption: "built by code from the canon",
      captionTitle: "The plain recap, built by code from the canon passages; no agent wrote it.",
    });
  }
  // Every chapter is a card, newest first: the written ones with their passage, the others with why and their passages.
  for (const ch of newest) {
    const o = () => setOpen(open?.id === ch.id && open.zone === "story" ? null : { id: ch.id, zone: "story" });
    storyCards.push(ch.canon ? chapterCard(ch, o) : missedCard(ch, o));
  }
  const storyOpen = open?.zone === "story" ? newest.find((c) => c.id === open.id) : undefined;
  // The Now card shows the relay's voice; the story names its arc's voice only where that differs or changed.
  const voiceNow = state?.voice ?? state?.voice_label?.replace(/^This relay's voice: /, "");
  const arcVoiceShown = arcInfo?.voice && (arcInfo.voice_changed_from || !voiceNow || arcInfo.voice !== voiceNow);
  const storyAside = [book && book.arcs.length > 1 && arcInfo ? `Arc ${arcInfo.arc + 1}` : "", arcVoiceShown ? arcVoiceLine(arcInfo) : ""].filter(Boolean).join(" · ");
  const practiceOpen = open?.zone === "practice" ? book?.practice_pages.find((c) => c.id === open.id) : undefined;
  return (
    <ViewBody>
      {off ? <WhatItIs title="This relay doesn't run the Lore." /> : null}
      {!off ? <ViewStatus loading={loading} error={error} empty={!book} /> : null}
      {now ? <CardRow title="Now" cards={[now]} /> : null}
      {book ? (
        <div className="e-book e-lore">
          {book.arcs.length > 1 ? (
            <nav aria-label="Arcs" className="e-book__arcs">
              <span className="e-faint">Arc</span>
              {[...book.arcs].sort((a, b) => b.arc - a.arc).map((a) => (
                <button key={a.arc} type="button" className="e-lab__link" aria-current={a.arc === shownArc ? "true" : undefined} onClick={() => { setOpen(null); setArc(a.arc); }}>
                  {arcLabel(a)}
                </button>
              ))}
            </nav>
          ) : null}
          {storyCards.length ? (
            <>
              <CardRow title="The story" aside={storyAside} cards={storyCards} />
              {storyOpen ? <ChapterDetail ch={storyOpen} navigate={navigate} onClose={() => setOpen(null)} /> : null}
            </>
          ) : null}
          {book.practice_pages.length ? (
            <section aria-label="Practice pages" className="e-lore__missed e-book__practice-sec">
              <details>
                <summary>Practice pages · {book.practice_pages.length}</summary>
                <p className="e-book__practice">{PRACTICE_LINE}.</p>
                <ul className="e-lore__missed-list">
                  {book.practice_pages.map((ch) => (
                    <li key={ch.id}>
                      <span className="e-lore__missed-name">{ch.label}</span>
                      <span className="e-book__flag">practice</span>
                      {ch.thin_jury ? <span className="e-book__flag e-book__flag--thin">jury of two</span> : null}{" "}
                      <span className="e-faint">{ch.canon ? clip(plainMarkdown(ch.canon.text), 120) : `No canon: ${noCanonReason(ch.reason)}.`}</span>{" "}
                      <button type="button" className="e-lab__link" onClick={() => setOpen({ id: ch.id, zone: "practice" })}>Details</button>
                    </li>
                  ))}
                </ul>
                {practiceOpen ? <ChapterDetail ch={practiceOpen} navigate={navigate} onClose={() => setOpen(null)} /> : null}
              </details>
            </section>
          ) : null}
          {empty ? <WhatItIs title="No chapter yet." /> : null}
          <HowItWorks />
        </div>
      ) : null}
    </ViewBody>
  );
}
