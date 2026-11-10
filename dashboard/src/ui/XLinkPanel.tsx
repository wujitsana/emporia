import React from "react";
import Text from "@kit/Text";
import type { AgentProfile } from "../api";
import { consoleUrl, httpFetch, VIA_CONSOLE } from "../transport";
import { VIEWER_ID } from "../relayEnv";
import { MetaGrid } from "./cards";
import {
  X_STYLES, accountUrl, bioLine, cleanHandle, linkStatus, postOnX, proofUrl, type XClaim, type XStyle,
} from "../xlink";

/** The bio (plain text, flagged when it reads like an instruction) and the X links on an agent's card. For the
 *  viewer's own agent, where the local console is in front, a "Post on X" button: it asks the console for the
 *  stock post (signed locally, sent nowhere) and opens X's own composer in a new tab. Read only. */
export function XLinkPanel({ agent }: { agent: AgentProfile }) {
  const bio = bioLine(agent);
  const links = agent.links ?? [];
  const own = VIA_CONSOLE && VIEWER_ID !== null && VIEWER_ID === agent.agent_id;
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
      {own ? <PostOnX agentId={agent.agent_id} /> : null}
    </div>
  );
}

function PostOnX({ agentId }: { agentId: string }) {
  const [handle, setHandle] = React.useState("");
  const [style, setStyle] = React.useState<XStyle>("plain");
  const [claim, setClaim] = React.useState<XClaim | null>(null);
  const [error, setError] = React.useState("");
  const clean = cleanHandle(handle);
  const prepare = async () => {
    setError("");
    setClaim(null);
    try {
      const r = await httpFetch(consoleUrl(`/x-claim/${encodeURIComponent(agentId)}?handle=${encodeURIComponent(clean)}&style=${style}`));
      if (!r.ok) throw new Error(`${r.status}`);
      setClaim(await r.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not make the post");
    }
  };
  const post = claim ? postOnX(claim) : null;
  return (
    <div className="e-xlink__post" style={{ marginTop: 10, marginBottom: 14 }}>
      <Text className="e-dim" style={{ fontSize: "0.68rem" }}>Link an X account</Text>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", marginTop: 4 }}>
        <input
          aria-label="X handle" placeholder="@handle" value={handle} maxLength={16}
          onChange={(e) => { setHandle(e.target.value); setClaim(null); }}
        />
        <select aria-label="post style" value={style} onChange={(e) => { setStyle(e.target.value as XStyle); setClaim(null); }}>
          {X_STYLES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <button type="button" disabled={!clean} onClick={prepare}>Make the post</button>
      </div>
      {error ? <Text className="e-faint">Could not make the post ({error}).</Text> : null}
      {claim && post ? (
        <div style={{ marginTop: 6 }}>
          <pre className="e-xlink__text" style={{ whiteSpace: "pre-wrap", margin: "4px 0" }}>{post.text}</pre>
          <Text className="e-faint" style={{ fontSize: 11 }}>{claim.post_length} of {claim.post_limit} characters</Text>
          <br />
          <a className="e-entity-chip" href={post.url} target="_blank" rel="noopener noreferrer">Post on X</a>
          <Text className="e-faint" style={{ display: "block", fontSize: 11, marginTop: 4 }}>
            Opens X's own composer in a new tab with this text: free, no API. After you post it, record it:
            {" "}<code>emporia agent x-link {agentId} --handle {claim.handle} --post POST_ID</code> (or paste the bio
            line, <code>bio</code>). Nothing is written to Emporia from here.
          </Text>
        </div>
      ) : null}
    </div>
  );
}
