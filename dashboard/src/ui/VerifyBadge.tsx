import Text from "@components/Text";
import { useInterval } from "../hooks";
import { consoleUrl, httpFetch, VIA_CONSOLE } from "../transport";
import { VIEWER_ID } from "../relayEnv";

interface VerifyReport {
  verdict: "ok" | "fail";
  failures: number;
  unchecked: number;
  findings: { check: string; status: string; detail: string }[];
}

async function fetchReport(sessionId: string): Promise<VerifyReport> {
  const r = await httpFetch(consoleUrl(
    `/verify/${encodeURIComponent(VIEWER_ID ?? "")}/${encodeURIComponent(sessionId)}`));
  if (!r.ok) throw new Error(`verify ${r.status}`);
  return r.json();
}

/**
 * P146: the game checked on this machine — signatures, the viewer's own
 * receipts, the result replayed — not the relay's word for it (AuditBadge
 * shows the relay's own claim). Only where the local console is in front:
 * served by the relay itself, there is nothing local to check with.
 */
export function VerifyBadge({ sessionId, live }: { sessionId: string; live: boolean }) {
  const [report, , error] = useInterval(() => fetchReport(sessionId), live ? 30_000 : 600_000, [sessionId]);
  if (!VIA_CONSOLE) return null;
  if (error && !report) return <Text className="e-faint" style={{ fontSize: 11 }} title={error}>? not checked here</Text>;
  if (!report) return null;
  const problems = report.findings.filter((f) => f.status === "fail").map((f) => f.detail);
  const title = report.verdict === "ok"
    ? `Checked on this machine: ${report.findings.length - report.unchecked} checks passed` +
      (report.unchecked ? `, ${report.unchecked} could not be checked` : "")
    : `Problems found:\n${problems.join("\n")}\n\nRun verify_session for an evidence file.`;
  return (
    <Text
      style={{ fontSize: 11, color: report.verdict === "ok" ? "var(--theme-focused-foreground)" : undefined }}
      className={report.verdict === "ok" ? undefined : "e-status-txt"}
      title={title}
    >
      {report.verdict === "ok" ? "✓ verified here" : `✗ ${report.failures} problem${report.failures === 1 ? "" : "s"}`}
    </Text>
  );
}
