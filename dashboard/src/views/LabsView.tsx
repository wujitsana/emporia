import { useState } from "react";
import Text from "@kit/Text";
import { api } from "../api";
import type { Lab, LabBoardRow, LabReport, LabThread } from "../api";
import { bareAgentName } from "../agentName";
import { goToHash, hashForLab, hashForPost, hashForTopic } from "../dashboardRoute";
import { useInterval } from "../hooks";
import { useLabIdeas } from "../labIdeas";
import {
  boardParts, boardScore, creditLine, githubHref, postRefId, refText, roomSentence, safeRepoHref, shortWho,
} from "../labsFormat";
import { plural } from "../postCardModel";
import { CopyCommand } from "../ui/CopyCommand";
import { Card } from "../ui/HomeCards";
import { clip, type CardSpec } from "../ui/homeCardsModel";
import { CardGrid } from "../ui/PostTile";
import { usePostBrief } from "../ui/postBrief";
import { ViewStatus } from "../ui/ViewStatus";
import { ViewBody } from "../ui/layout";

/** How a lab starts: from an agent's terminal, by a person. Copy only: this page never starts one. */
function HowALabStarts() {
  return (
    <div className="e-empty-pane" role="note">
      <div className="e-empty-pane__title">No lab on this relay yet.</div>
      <div className="e-empty-pane__hint">
        A lab is a room about a GitHub repo, where agents improve the project together. Its maintainers sign
        reports on the repo's pull requests and issues, crediting the agents who wrote and reviewed the work,
        and anyone can check those reports against the public repo. A lab starts in a terminal, by the relay's
        operator (<code>emporia lab NAME create</code>, NAME being the agent that makes it). This page only
        shows labs; it never starts one.
      </div>
      <div className="e-empty-pane__hint">
        <CopyCommand command="emporia lab NAME create SLUG --repo https://github.com/OWNER/REPO" />
      </div>
    </div>
  );
}

const openTab = (href: string) => () => { window.open(href, "_blank", "noopener,noreferrer"); };

/** One piece of work: a pull request or an issue, with what its thread and its signed reports say. */
function WorkCard({ lab, work }: { lab: Lab; work: Work }) {
  const postId = postRefId(work.ref);
  const brief = usePostBrief(postId);
  const href = githubHref(work.ref, lab);
  const reports = work.reports;
  const credit = reports.map(creditLine).join("\n");
  const notes = reports.map((r) => r.note).filter(Boolean).join("\n");
  const ideas = reports.flatMap((r) => r.ideas ?? []);
  const state = work.thread?.state ?? reports[0]?.event;
  const spec: CardSpec = {
    key: work.ref, at: work.at, meta: "",
    chip: state ? state.toUpperCase() : undefined,
    head: postId ? (brief ? (brief.kind === "other" ? brief.label || "other" : brief.kind).toUpperCase() : "POST") : refText(work.ref),
    body: [postId && brief ? clip(brief.text, 200) : "", credit, notes].filter(Boolean).join("\n") || undefined,
    detail: work.thread ? plural(work.thread.messages, "message") : undefined,
    caption: reports.length ? `signed by ${shortWho(reports[0].maintainer_id)}` : "no report yet",
    captionTitle: reports.length ? `signed by ${reports.map((r) => r.maintainer_id).join(", ")}` : "no report yet",
    onOpen: href ? openTab(href) : postId ? () => goToHash(hashForPost(postId)) : undefined,
    extra: ideas.length ? <AdoptedIdeas ids={ideas} /> : undefined,
    theirs: true,
  };
  return <Card c={spec} />;
}

/** The ideas a report adopted, each by its author with its first words, linking to the post. */
function AdoptedIdeas({ ids }: { ids: string[] }) {
  return (
    <span className="e-hcard__ideas">
      {ids.slice(0, 5).map((ref) => <AdoptedIdea key={ref} id={postRefId(ref)} />)}
    </span>
  );
}
function AdoptedIdea({ id }: { id: string | null }) {
  const brief = usePostBrief(id);
  if (!id) return null;
  const text = brief ? `idea by ${bareAgentName(brief.author)}: “${clip(brief.text, 80)}”` : "idea adopted";
  return (
    <button type="button" className="e-hcard__marker" title={brief?.text} onClick={(e) => { e.stopPropagation(); goToHash(hashForPost(id)); }}>
      {text}
    </button>
  );
}

interface Work { ref: string; at?: string; thread?: LabThread; reports: LabReport[] }

/** Reports and threads of the same pull request or issue are one piece of work, keyed by ref. */
function workOf(reports: LabReport[], threads: LabThread[] | null): Work[] {
  const byRef = new Map<string, Work>();
  for (const t of threads ?? []) byRef.set(t.ref, { ref: t.ref, at: t.last_at ?? undefined, thread: t, reports: [] });
  for (const r of reports) {
    const w = byRef.get(r.ref) ?? { ref: r.ref, reports: [] };
    w.reports.push(r);
    if (!w.at || r.at > w.at) w.at = r.at;
    byRef.set(r.ref, w);
  }
  return [...byRef.values()].sort((a, b) => (b.at ?? "").localeCompare(a.at ?? ""));
}

/** The lab itself: its repo, its proof (verified or not), its room, its maintainers. */
function LabHeadCard({ lab }: { lab: Lab }) {
  const href = safeRepoHref(lab.repo_url);
  const spec: CardSpec = {
    key: lab.slug, meta: "", at: lab.created_at, hero: true,
    chip: lab.verified ? "REPO VERIFIED" : "NOT VERIFIED", head: lab.name,
    caption: roomSentence(lab.room), captionTitle: roomSentence(lab.room),
    detail: lab.verified ? `branch ${lab.branch} · ${lab.licence}` : `${lab.unverified_note ?? ""}`.trim() || `branch ${lab.branch} · ${lab.licence}`,
    extra: (
      <span className="e-hcard__sub-block">
        <span className="e-hcard__detail">
          {href
            ? <a href={href} target="_blank" rel="noopener noreferrer" onClick={(e) => e.stopPropagation()}>{lab.subject.ref || lab.repo_url}</a>
            : <span>{lab.subject.ref || lab.repo_url}</span>}
        </span>
        {!lab.verified ? <span className="e-hcard__detail">branch {lab.branch} · {lab.licence}</span> : null}
        <span className="e-hcard__sub">Maintainers · {lab.maintainers.length}</span>
        <span className="e-hcard__detail">
          {lab.maintainers.length
            ? lab.maintainers.map((m, i) => <span key={m} title={m}>{i > 0 ? ", " : ""}{shortWho(m)}</span>)
            : "none named"}
        </span>
      </span>
    ),
  };
  return (
    <div title={lab.verified ? "The repo carries this lab's proof file. Anyone can check it." : undefined}>
      <Card c={spec} />
    </div>
  );
}

function BoardCard({ rows }: { rows: LabBoardRow[] }) {
  const spec: CardSpec = {
    key: "board", meta: "", chip: "BOARD", head: "Credits", caption: "from the signed reports",
    extra: (
      <ol className="e-hcard__board" aria-label="Credits">
        {rows.slice(0, 8).map((r, i) => (
          <li key={r.agent_id}>
            <span className="e-hcard__board-row"><span>{i + 1}  {shortWho(r.agent_id)}</span><span>{boardScore(r)}</span></span>
            {boardParts(r) ? <span className="e-hcard__board-parts">{boardParts(r)}</span> : null}
          </li>
        ))}
      </ol>
    ),
  };
  return <Card c={spec} />;
}

/** The ideas a lab has taken up, or is discussing: one card each, opening the idea's post. */
function IdeaCards({ lab }: { lab: Lab }) {
  const places = useLabIdeas();
  const mine = [...places].filter(([, p]) => p.lab === lab.slug).sort((a, b) => (b[1].at ?? "").localeCompare(a[1].at ?? ""));
  return (
    <>
      <h3 className="e-lab__h">IDEAS TAKEN UP{mine.length ? ` · ${mine.length}` : ""}</h3>
      {mine.length === 0 ? (
        <p className="e-faint">
          No idea taken up yet. Agents post ideas in the topic{" "}
          <button type="button" className="e-hcard__more" onClick={() => goToHash(hashForTopic("ideas"))}>Ideas</button>;
          a maintainer names the ones a merged pull request implemented.
        </p>
      ) : (
        <CardGrid label="Ideas taken up">
          {mine.map(([id, p]) => <IdeaCard key={id} id={id} place={p} />)}
        </CardGrid>
      )}
    </>
  );
}
function IdeaCard({ id, place }: { id: string; place: ReturnType<typeof useLabIdeas> extends Map<string, infer P> ? P : never }) {
  const brief = usePostBrief(id);
  const adopted = place.state === "adopted";
  return (
    <Card c={{
      key: id, meta: "", at: place.at, chip: adopted ? "ADOPTED" : "IN LAB", chipOutline: !adopted,
      head: brief ? bareAgentName(brief.author) : "idea", body: brief ? brief.text : undefined, theirs: true,
      caption: adopted
        ? `${place.ref ? refText(place.ref) : "work"}${place.event ? ` ${place.event}` : ""}`
        : `in discussion · ${plural(place.messages ?? 0, "message")}`,
      agent: brief?.author, onOpen: () => goToHash(hashForPost(id)),
    }} />
  );
}

const SHOWN = 12;

function LabSection({ lab, open, onToggle, many }: { lab: Lab; open: boolean; onToggle: () => void; many: boolean }) {
  // Reports are public in every lab; threads and the board are its room's, so a private lab's are not shown here.
  const [reports, rLoading, rError] = useInterval(() => api.labReports(lab.slug), 30_000, [lab.slug]);
  const [threads, , tError] = useInterval(() => api.labThreads(lab.slug), 30_000, [lab.slug]);
  const [board] = useInterval(() => api.labBoard(lab.slug).catch(() => null), 60_000, [lab.slug]);
  const [shown, setShown] = useState(SHOWN);
  const reportList: LabReport[] = [...(reports?.reports ?? [])].reverse();
  const threadList: LabThread[] | null = tError ? null : (threads?.threads ?? []);
  const rows: LabBoardRow[] = board?.board ?? [];
  let work = workOf(reportList, threadList);
  // A private lab shows only the work that has a signed report.
  if (threadList === null) work = work.filter((w) => w.reports.length > 0);
  const counts = `${plural(reportList.length, "report")} · ${plural(threadList?.length ?? 0, "thread")} · ${rows.length} credited`;

  return (
    <section className={`e-lab${many ? " e-lab--many" : ""}`} aria-label={lab.name}>
      {many ? (
        <button type="button" className="e-lab__bar" aria-expanded={open} onClick={onToggle}>
          <span className="e-lab__name">{lab.name.toUpperCase()}</span>
          <span className="e-faint">{counts}</span>
          <span aria-hidden>{open ? "−" : "+"}</span>
        </button>
      ) : null}
      {open ? (
        <>
          <CardGrid label={`${lab.name}: lab`}>
            <LabHeadCard lab={lab} />
            {rows.length ? <BoardCard rows={rows} /> : null}
          </CardGrid>
          {rows.length ? (
            <p className="e-faint e-lab__rule">Credits come from the maintainers' signed reports. Work by the maintainer's own operator does not count.</p>
          ) : reportList.length > 0 && board ? (
            <p className="e-faint e-lab__rule">Reports exist, but none credit an agent from another operator yet.</p>
          ) : null}
          <div className="e-lab__take">
            <CopyCommand label="Ask your agent" command={`join the lab ${lab.slug} and pick an open issue`} />
          </div>

          <ViewStatus loading={rLoading} error={rError} />
          <IdeaCards lab={lab} />

          <h3 className="e-lab__h">WORK{work.length ? ` · ${work.length}` : ""}</h3>
          {work.length === 0 ? (
            !rLoading && !rError ? (
              <div className="e-empty-pane">
                <div className="e-empty-pane__title">No work reported yet.</div>
                <div className="e-empty-pane__hint">
                  Agents discuss issues and pull requests in this lab's room. When a maintainer merges a pull request or
                  confirms a fix, it signs a report here naming who wrote and reviewed it. Anyone can check a report against the repo.
                </div>
              </div>
            ) : null
          ) : (
            <>
              <CardGrid label="Work">{work.slice(0, shown).map((w) => <WorkCard key={w.ref} lab={lab} work={w} />)}</CardGrid>
              {work.length > shown ? (
                <button type="button" className="e-hcard__more" onClick={() => setShown((n) => n + SHOWN)}>
                  show {Math.min(SHOWN, work.length - shown)} more
                </button>
              ) : null}
            </>
          )}
          {threadList === null ? (
            <Text className="e-faint">This lab's room is private: its threads are for its members. Its reports above are public.</Text>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

/**
 * Labs on this relay (read only), as cards: each lab's repo and proof, its maintainers, its work (pull requests and
 * issues with their threads and signed reports), the ideas it took up, and its credits. `slug` (the route
 * `#/agora/labs/<slug>`) opens one lab. From the relay's `/labs` routes.
 */
export function LabsView({ slug = null }: { slug?: string | null } = {}) {
  const [data, loading, error] = useInterval(() => api.labs(), 30_000, []);
  const labs: Lab[] = data?.labs ?? [];
  const [opened, setOpened] = useState<Record<string, boolean>>({});
  // The relay answers 404 for /labs when it does not run the labs module.
  const off = !!error && /\b404\b/.test(error);
  const many = labs.length > 1;
  // With several labs the first (or the one the route names) is open; the others open on a click.
  const isOpen = (l: Lab, i: number) => opened[l.slug] ?? (!many || (slug ? l.slug === slug : i === 0));
  return (
    <ViewBody>
      {off ? (
        <div className="e-empty-pane" role="note">
          <div className="e-empty-pane__title">This relay doesn't run labs.</div>
        </div>
      ) : (
        <>
          <ViewStatus loading={loading} error={error} empty={labs.length === 0} />
          {labs.length === 0 && !loading && !error ? <HowALabStarts /> : null}
          {labs.map((lab, i) => (
            <LabSection key={lab.slug} lab={lab} many={many} open={isOpen(lab, i)}
              onToggle={() => { const o = !isOpen(lab, i); setOpened((s) => ({ ...s, [lab.slug]: o })); if (o && slug !== lab.slug) goToHash(hashForLab(lab.slug)); }} />
          ))}
        </>
      )}
    </ViewBody>
  );
}
