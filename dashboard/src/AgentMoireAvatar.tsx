import { useId, useMemo } from "react";
import { useAgentNav } from "./agentNavCtx";
import { everydayAvatar } from "./avatars";
import { pictureId, rememberStableId, stableOf, useChainPicture } from "./agentPicture";

/** The inside of a generated SVG (its outer tag removed), so the host can set the size. The string comes from
 *  `avatars.ts` (literal tables and integers only), never from an agent's text. */
function svgInner(svg: string): string {
  return svg.slice(svg.indexOf(">") + 1, svg.lastIndexOf("</svg>"));
}

/** An agent's picture: the everyday picture drawn from its stable id (SHA-256 of the handle where only the handle is
 *  known), or, for an agent whose ERC-8004 link this console has already proven, the identity token's on-chain image. */
export function AgentMoireAvatar({
  agentId,
  stableId,
  size = 28,
  link = true,
}: {
  agentId: string;
  /** The agent's 64-hex stable id, where the caller has it; else the picture is drawn from the handle's hash. */
  stableId?: string | null;
  size?: number;
  /** The glyph opens the agent's page (when App provides the way); false on the page itself. */
  link?: boolean;
}) {
  const nav = useAgentNav();
  rememberStableId(agentId, stableId);
  const pid = pictureId(agentId, stableId);
  /* Pattern ids are unique per instance, so many pictures on one page never share a pattern. */
  const uid = pid.slice(0, 12) + useId().replace(/[^a-zA-Z0-9]/g, "");
  const inner = useMemo(() => svgInner(everydayAvatar(pid, uid)), [pid, uid]);
  const onChain = useChainPicture(stableOf(agentId, stableId));

  const glyph = onChain ? (
    <img className="e-moire-avatar" src={onChain} width={size} height={size} alt="" aria-hidden
      referrerPolicy="no-referrer" decoding="async" data-testid="avatar-onchain" style={{ objectFit: "cover" }} />
  ) : (
    <svg
      width={size}
      height={size}
      className="e-moire-avatar"
      viewBox="0 0 32 32"
      aria-hidden
      dangerouslySetInnerHTML={{ __html: inner }}
    />
  );
  if (!link || !nav) return glyph;
  const open = (e: { stopPropagation: () => void }) => { e.stopPropagation(); nav("agents", { agentId }); };
  return (
    <span className="e-agent-link e-agent-link--glyph" role="link" tabIndex={0} aria-label={`Open ${agentId}'s page`}
      onClick={open} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); open(e); } }}>
      {glyph}
    </span>
  );
}
