import Text from "@kit/Text";
import { api } from "../api";
import type { Lab, LabReport, LabThread } from "../api";
import { useInterval } from "../hooks";
import { creditLine, refText, roomLine, safeRepoHref, shortWho } from "../labsFormat";
import { whenText } from "../relayFormat";
import { CopyCommand } from "../ui/CopyCommand";
import { SlimCard } from "../ui/cards";
import { RowTable, type RowCells } from "../ui/RowTable";
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

function LabCard({ lab }: { lab: Lab }) {
  // Reports are public in every lab; threads are its room's, so a private lab's are not shown here.
  const [reports, rLoading, rError] = useInterval(() => api.labReports(lab.slug), 30_000, [lab.slug]);
  const [threads, , tError] = useInterval(() => api.labThreads(lab.slug), 30_000, [lab.slug]);
  const reportList: LabReport[] = [...(reports?.reports ?? [])].reverse();
  const threadList: LabThread[] = threads?.threads ?? [];
  const href = safeRepoHref(lab.repo_url);

  const reportRows: RowCells[] = reportList.map((r) => [
    refText(r.ref),
    <span key="e" className="e-badge e-badge--dim">{r.event}</span>,
    <span key="c" className="e-dim" style={{ fontSize: 11 }}>{creditLine(r)}</span>,
    <span key="m" className="e-faint" style={{ fontSize: 11 }}>{shortWho(r.maintainer_id)}</span>,
    <span key="w" className="e-faint" style={{ fontSize: 11 }}>{whenText(r.at)}</span>,
  ]);
  const threadRows: RowCells[] = threadList.slice(0, 12).map((t) => [
    refText(t.ref),
    <span key="s" className="e-badge e-badge--dim">{t.state}</span>,
    String(t.messages),
    <span key="w" className="e-faint" style={{ fontSize: 11 }}>{t.last_at ? whenText(t.last_at) : "—"}</span>,
  ]);

  return (
    <SlimCard
      title={lab.name}
      action={<Text className="e-dim">{lab.verified ? "repo verified" : "repo not verified"}</Text>}
    >
      <Text className="e-dim" style={{ display: "block" }}>
        {href
          ? <a href={href} target="_blank" rel="noopener noreferrer">{lab.subject.ref || lab.repo_url}</a>
          : <span>{lab.subject.ref || lab.repo_url}</span>}
        {" · "}branch {lab.branch} · {lab.licence} · {roomLine(lab.room)}
      </Text>
      {!lab.verified && lab.unverified_note ? <Text className="e-faint" style={{ display: "block" }}>{lab.unverified_note}</Text> : null}
      <Text className="e-subhead" style={{ display: "block", marginTop: 10 }}>Maintainers · {lab.maintainers.length}</Text>
      <Text className="e-dim" style={{ display: "block", fontSize: 12 }}>
        {lab.maintainers.length ? lab.maintainers.map(shortWho).join(", ") : "none named"}
      </Text>

      <Text className="e-subhead" style={{ display: "block", marginTop: 10 }}>
        Signed reports{reportList.length ? ` · ${reportList.length}` : ""}
      </Text>
      <ViewStatus loading={rLoading} error={rError} />
      {reportList.length === 0 ? (
        !rLoading && !rError ? <Text className="e-faint">No report yet. A maintainer signs one when a pull request is merged or an issue is fixed.</Text> : null
      ) : (
        <RowTable headers={["REF", "EVENT", "CREDITED", "BY", "WHEN"]} rows={reportRows} keys={reportList.map((r) => r.report_id)} />
      )}

      <Text className="e-subhead" style={{ display: "block", marginTop: 10 }}>
        Threads{threadList.length ? ` · ${threadList.length}` : ""}
      </Text>
      {tError ? (
        <Text className="e-faint">This lab's room is private: its threads are for its members. Its reports above are public.</Text>
      ) : threadList.length === 0 ? (
        <Text className="e-faint">No thread yet. Each issue or pull request gets one in the lab's room.</Text>
      ) : (
        <RowTable headers={["REF", "STATE", "MSGS", "LAST"]} rows={threadRows} keys={threadList.slice(0, 12).map((t) => t.ref)} />
      )}
    </SlimCard>
  );
}

/**
 * Labs on this relay (read only): each lab's repo, its maintainers, its threads and the public
 * signed reports with what they credited. From the relay's public `/labs` routes.
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
          {labs.map((lab) => <LabCard key={lab.slug} lab={lab} />)}
        </>
      )}
    </ViewBody>
  );
}
