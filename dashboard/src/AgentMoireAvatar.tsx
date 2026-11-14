import { useAgentNav } from "./agentNavCtx";

/* 10 well-separated hues matching the chat palette; none in the amber band (20-70 degrees) */
const MOIRE_HUES = [210, 0, 145, 280, 235, 175, 300, 195, 255, 100] as const;

function hashSeed(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** Deterministic moiré tile per agent_id */
export function AgentMoireAvatar({
  agentId,
  size = 28,
  link = true,
}: {
  agentId: string;
  size?: number;
  /** The glyph opens the agent's page (when App provides the way); false on the page itself. */
  link?: boolean;
}) {
  const nav = useAgentNav();
  const seed = hashSeed(agentId || "?");
  const hue = MOIRE_HUES[seed % MOIRE_HUES.length];
  /* Rotation angles spaced well apart — avoid near-parallel lines */
  const rotA = ((seed % 7) * 13) - 40;       // -40 … +51
  const rotB = rotA + 55 + (seed % 20);       // always 55–75° offset from A
  const safe = agentId.replace(/[^a-zA-Z0-9_-]/g, "").slice(0, 12) || "x";
  const idA = `moire-a-${safe}-${size}`;
  const idB = `moire-b-${safe}-${size}`;

  const glyph = (
    <svg
      width={size}
      height={size}
      className="e-moire-avatar"
      viewBox="0 0 32 32"
      aria-hidden
    >
      <defs>
        {/* patternUnits="userSpaceOnUse" so width/height are in px, not fraction of object */}
        <pattern id={idA} width="4" height="4" patternUnits="userSpaceOnUse" patternTransform={`rotate(${rotA}, 16, 16)`}>
          <line x1="0" y1="0" x2="0" y2="4" stroke={`hsl(${hue}, 70%, 60%)`} strokeWidth="1" />
        </pattern>
        <pattern id={idB} width="4" height="4" patternUnits="userSpaceOnUse" patternTransform={`rotate(${rotB}, 16, 16)`}>
          <line x1="0" y1="0" x2="4" y2="0" stroke={`hsl(${(hue + 72) % 360}, 60%, 68%)`} strokeWidth="0.8" />
        </pattern>
      </defs>
      <rect width="32" height="32" fill={`hsl(${hue}, 28%, 18%)`} />
      <rect width="32" height="32" fill={`url(#${idA})`} opacity="0.85" />
      <rect width="32" height="32" fill={`url(#${idB})`} opacity="0.65" />
    </svg>
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
