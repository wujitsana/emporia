import type { View } from "./navConfig";
import type { NavOpts } from "./eventNav";

export type Navigate = (view: View, opts?: NavOpts) => void;

export const EMPTY_SEED_HINT =
  "No data yet. Seed the relay: emporia/scripts/seed_demo_relay.py (EMPORIA_GAMES_DB must match relay HOME).";