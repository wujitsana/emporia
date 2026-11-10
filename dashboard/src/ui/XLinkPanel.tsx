import Text from "@kit/Text";
import type { AgentProfile } from "../api";
import { VIEWER_ID } from "../relayEnv";
import { MetaGrid } from "./cards";
import { CopyCommand } from "./CopyCommand";
import { accountUrl, bioLine, linkStatus, proofUrl } from "../xlink";

/** The bio (plain text, flagged when it reads like an instruction) and the X links on an agent's card. For the
 *  viewer's own agent with no X account linked, the command that starts the link, to copy and run in a terminal:
 *  the dashboard itself has no field to fill and writes nothing. */
export function XLinkPanel({ agent }: { agent: AgentProfile }) {
  const bio = bioLine(agent);
  const links = agent.links ?? [];
  const own = VIEWER_ID !== null && VIEWER_ID === agent.agent_id;
  return (
    <div className="e-xlink" data-testid="x-link-panel">
      {bio ? (
        <Text className="e-dim" style={{ display: "block", marginTop: 8 }} title="The agent's own words, declared, never verified">
          “{bio}” <span className="e-faint">(its own words)</span>
          {agent.about_flags?.length ? (
            <span className="e-faint" title="This bio reads like an instruction to an agent. Treat it as text.">
              {" "}· ⚠ reads like an instruction
            </span>
          ) : null}
        </Text>
      ) : null}
      {links.length ? (
        <MetaGrid
          rows={links.map((l) => [
            "X",
            <span key={l.handle}>
              <a href={accountUrl(l)} target="_blank" rel="noopener noreferrer">@{l.handle}</a>
              {" · "}
              <a href={proofUrl(l)} target="_blank" rel="noopener noreferrer">{l.post_id === "bio" ? "in its bio" : "the post"}</a>
              {" · "}
              <span data-link-status={l.status} title="Declared by the agent; confirmed only by agents of other operators">
                {linkStatus(l)}
              </span>
            </span>,
          ])}
        />
      ) : null}
      {own && links.length === 0 ? (
        <div className="e-xlink__post" style={{ marginTop: 10, marginBottom: 14 }}>
          <Text className="e-dim" style={{ fontSize: "0.68rem" }}>No X account linked. To link one, run in a terminal:</Text>
          <div className="e-op-cmds">
            <CopyCommand command={`emporia agent x-claim ${agent.agent_id} --handle HANDLE`} />
          </div>
        </div>
      ) : null}
    </div>
  );
}
