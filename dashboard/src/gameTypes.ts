// The game and job module ids the dashboard names, with their labels. Pinned against the relay's
// MODULE_REGISTRY by test_dashboard_game_types_match_the_relay_module_registry, which reads this
// file's source text. Set plays first; chess last among the games and never the default; jobs
// belong to Deals (docs/ui-ux-structure.md).
export const GAME_TYPES = [
  // Calls: the set plays are `:v2` (the `:v1` ones are retired; a chip matches either by name).
  { id: "emporia:blotto:v2", label: "blotto", kind: "set" },
  { id: "emporia:two-thirds:v2", label: "⅔ average", kind: "set" },
  { id: "emporia:lowest-unique:v2", label: "lowest unique", kind: "set" },
  { id: "emporia:bargain:v2", label: "bargain", kind: "set" },
  { id: "emporia:liars-dice:v1", label: "liar's dice", kind: "set" },
  { id: "emporia:cv-auction:v1", label: "auction", kind: "set" },
  { id: "emporia:chess:v1", label: "chess", kind: "chess" },
  { id: "emporia:service:v1", label: "service", kind: "job" },
  { id: "emporia:code-review:v1", label: "code review", kind: "job" },
  { id: "emporia:research:v1", label: "research", kind: "job" },
] as const;
