import Text from "@kit/Text";
import { api } from "../api";
import type { Lab, LabBoardRow, LabReport, LabThread } from "../api";
import { useInterval } from "../hooks";
import { boardLine, creditLine, refText, roomLine, safeRepoHref, shortWho } from "../labsFormat";
import { whenText } from "../relayFormat";
import { CopyCommand } from "../ui/CopyCommand";
import { Card } from "../ui/HomeCards";
import type { CardSpec } from "../ui/homeCardsModel";
import { CardGrid } from "../ui/PostTile";
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

const reportCard = (r: LabReport): CardSpec => ({
  key: r.report_id, meta: "signed report", at: r.at, chip: r.event.toUpperCase(), head: refText(r.ref),
  body: creditLine(r), caption: `signed by ${shortWho(r.maintainer_id)}`, theirs: true,
});

const threadCard = (t: LabThread): CardSpec => ({
  key: t.ref, meta: "thread", at: t.last_at ?? undefined, time: t.last_at ? undefined : "no message yet", chip: t.state.toUpperCase(),
  head: refText(t.ref), detail: `${t.messages} ${t.messages === 1 ? "message" : "messages"}${t.last_at ? ` · last ${whenText(t.last_at)}` : ""}`,
  caption: t.reports.length ? `reports: ${t.reports.join(", ")}` : "no report yet", theirs: true,
});

/** The lab itself: its repo, its proof (verified or not), its room, its maintainers. */
function LabHeadCard({ lab }: { lab: Lab }) {
  const href = safeRepoHref(lab.repo_url);
  const spec: CardSpec = {
    key: lab.slug, meta: `lab · ${lab.branch} · ${lab.licence}`, at: lab.created_at, hero: true,
    chip: lab.verified ? "REPO VERIFIED" : "NOT VERIFIED", head: lab.name,
    caption: roomLine(lab.room), foot: lab.verified ? undefined : lab.unverified_note,
    extra: (
      <span className="e-hcard__sub-block">
        <span className="e-hcard__detail">
          {href
            ? <a href={href} target="_blank" rel="noopener noreferrer">{lab.subject.ref || lab.repo_url}</a>
            : <span>{lab.subject.ref || lab.repo_url}</span>}
        </span>
        <span className="e-hcard__sub">Maintainers · {lab.maintainers.length}</span>
        <span className="e-hcard__detail">{lab.maintainers.length ? lab.maintainers.map(shortWho).join(", ") : "none named"}</span>
      </span>
    ),
  };
  return <Card c={spec} />;
}

function BoardCard({ rows, rule }: { rows: LabBoardRow[]; rule?: string }) {
  const spec: CardSpec = {
    key: "board", meta: "credits board", chip: "BOARD", head: "Who is credited", caption: "from the signed reports", detail: rule,
    extra: (
      <ol className="e-hcard__list" aria-label="Credits">
        {rows.slice(0, 8).map((r) => (
          <li key={r.agent_id}><span>{shortWho(r.agent_id)}</span><span>{boardLine(r)}</span></li>
        ))}
      </ol>
    ),
  };
  return <Card c={spec} />;
}

function LabSection({ lab }: { lab: Lab }) {
  // Reports are public in every lab; threads and the board are its room's, so a private lab's are not shown here.
  const [reports, rLoading, rError] = useInterval(() => api.labReports(lab.slug), 30_000, [lab.slug]);
  const [threads, , tError] = useInterval(() => api.labThreads(lab.slug), 30_000, [lab.slug]);
  const [board] = useInterval(() => api.labBoard(lab.slug).catch(() => null), 60_000, [lab.slug]);
  const reportList: LabReport[] = [...(reports?.reports ?? [])].reverse();
  const threadList: LabThread[] = threads?.threads ?? [];
  const rows: LabBoardRow[] = board?.board ?? [];

  return (
    <section className="e-lab" aria-label={lab.name}>
      <CardGrid label={`${lab.name}: lab`}>
        <LabHeadCard lab={lab} />
        {rows.length ? <BoardCard rows={rows} rule={board?.rule} /> : null}
      </CardGrid>

      <Text className="e-subhead" style={{ display: "block", marginTop: 6 }}>
        Signed reports{reportList.length ? ` · ${reportList.length}` : ""}
      </Text>
      <ViewStatus loading={rLoading} error={rError} />
      {reportList.length === 0 ? (
        !rLoading && !rError ? <Text className="e-faint">No report yet. A maintainer signs one when a pull request is merged or an issue is fixed.</Text> : null
      ) : (
        <CardGrid label="Signed reports">{reportList.map((r) => <Card key={r.report_id} c={reportCard(r)} />)}</CardGrid>
      )}

      <Text className="e-subhead" style={{ display: "block", marginTop: 6 }}>
        Threads{threadList.length ? ` · ${threadList.length}` : ""}
      </Text>
      {tError ? (
        <Text className="e-faint">This lab's room is private: its threads are for its members. Its reports above are public.</Text>
      ) : threadList.length === 0 ? (
        <Text className="e-faint">No thread yet. Each issue or pull request gets one in the lab's room.</Text>
      ) : (
        <CardGrid label="Threads">
          {threadList.slice(0, 12).map((t) => <Card key={t.ref} c={threadCard(t)} />)}
        </CardGrid>
      )}
    </section>
  );
}

/**
 * Labs on this relay (read only), as cards: each lab's repo and proof, its maintainers, the public signed
 * reports with what they credited, its threads and its credits board. From the relay's `/labs` routes.
 */
export function LabsView() {
  const [data, loading, error] = useInterval(() => api.labs(), 30_000, []);
  const labs: Lab[] = data?.labs ?? [];
  // The relay answers 404 for /labs when it does not run the labs module.
  const off = !!error && /\b404\b/.test(error);
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
          {labs.map((lab) => <LabSection key={lab.slug} lab={lab} />)}
        </>
      )}
    </ViewBody>
  );
}
