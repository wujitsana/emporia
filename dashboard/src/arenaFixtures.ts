import type { Session, SessionAction } from "./api";

/** Action logs for each game type, as the relay's /sessions/<id>/actions returns them. */
const H = "ab".repeat(32);
let n = 0;
export const act = (agent_id: string, action_type: string, payload: Record<string, unknown> = {}, artifacts?: unknown): SessionAction => ({
  action_id: `a${++n}`, agent_id, action_type, payload, created_at: "2026-11-03T12:00:00Z",
  result: { success: true, ...(artifacts ? { artifacts } : {}) },
});

export const session = (module_type: string, data: Record<string, unknown>, status = "completed"): Session => ({
  session_id: `sess_${module_type.split(":")[1]}_0123456789`, module_type, status, participants: ["alpha", "beta"],
  current_agent: "alpha", step_number: 4, created_at: "2026-11-03T12:00:00Z", state: { data },
});

export const BLOTTO = {
  session: session("emporia:blotto:v2", {
    battlefields: 5, troops: 100, rounds: 1, round: 1, phase: "reveal", wins: { alpha: 1 },
    history: [{ round: 1, allocations: { alpha: [30, 30, 30, 5, 5], beta: [10, 10, 10, 35, 35] }, winner: "alpha" }],
  }),
  actions: [
    act("alpha", "commit", { commitment: H }), act("beta", "commit", { commitment: H }),
    act("alpha", "reveal", { move: [30, 30, 30, 5, 5], nonce: "n1" }),
    act("beta", "reveal", { move: [10, 10, 10, 35, 35], nonce: "n2" }, { result: { winner: "alpha", outcome_type: "win" } }),
  ],
};

export const TWO_THIRDS = {
  session: session("emporia:two-thirds:v2", { phase: "reveal", guesses: { alpha: 20, beta: 60 }, commitments: { alpha: H, beta: H } }),
  actions: [
    act("alpha", "commit", { commitment: H }), act("beta", "commit", { commitment: H }),
    act("alpha", "reveal", { move: 20, nonce: "n1" }),
    act("beta", "reveal", { move: 60, nonce: "n2" }, { result: { winner: "alpha", outcome_type: "win" } }),
  ],
};

export const DICE = {
  session: session("emporia:liars-dice:v1", {
    players: 2, dice: 3, bids: [{ n: 1, by: "alpha", quantity: 2, face: 4 }, { n: 2, by: "beta", quantity: 4, face: 4 }],
    ended: { how: "liar", by: "alpha", bid: 2, bidder: "beta" }, turn_order: ["alpha", "beta"],
    rand: { reveals: { alpha: {}, beta: {} } },
    deal: { hands: { alpha: { values: [1, 4, 6] }, beta: { values: [2, 2, 5] } } },
  }),
  actions: [
    act("alpha", "bid", { quantity: 2, face: 4 }), act("beta", "bid", { quantity: 4, face: 4 }), act("alpha", "liar"),
    act("alpha", "reveal", { s: "ss", nonce: "nn" }),
    act("beta", "reveal", { s: "tt", nonce: "mm" }, { result: { winner: "alpha", outcome_type: "won", termination: "bid_failed", count: 1, ones_wild: false, points: { alpha: 1, beta: 0 } } }),
  ],
};

export const AUCTION = {
  session: session("emporia:cv-auction:v1", {
    players: 2, spread: 10, phase: "secrets", base_value: 40, commitments: { alpha: H, beta: H }, revealed: { alpha: 52, beta: 61 },
    rand: { reveals: { alpha: {}, beta: {} } },
    deal: { hands: { alpha: { values: 47 }, beta: { values: 53 } } },
  }),
  actions: [
    act("alpha", "commit", { commitment: H }), act("beta", "commit", { commitment: H }),
    act("alpha", "reveal", { move: 52, nonce: "n1" }), act("beta", "reveal", { move: 61, nonce: "n2" }),
    act("alpha", "reveal", { s: "ss", nonce: "nn" }),
    act("beta", "reveal", { s: "tt", nonce: "mm" }, { result: {
      winner: null, outcome_type: "draw", termination: "winners_curse", value: 55, base_value: 40, bids: { alpha: 52, beta: 61 },
      winning_bid: 61, bidder: "beta", profit: -6, points: { alpha: 0, beta: -6 }, values: { alpha: 47, beta: 53 },
    } }),
  ],
};

export const BARGAIN = {
  session: session("emporia:bargain:v2", {
    pool: { silk: 4, spice: 3, amber: 3 }, total: 10, max_offers: 6,
    offers: [
      { n: 1, by: "alpha", allocation: { alpha: { silk: 4, spice: 1, amber: 0 }, beta: { silk: 0, spice: 2, amber: 3 } } },
      { n: 2, by: "beta", allocation: { beta: { silk: 1, spice: 2, amber: 3 }, alpha: { silk: 3, spice: 1, amber: 0 } } },
    ],
    ended: { how: "accepted", by: "alpha", offer: 2 },
    deal: { hands: { alpha: { values: { silk: 5, spice: 1, amber: 0 } }, beta: { values: { silk: 0, spice: 3, amber: 4 } } } },
  }),
  actions: [
    act("alpha", "offer", { keep: { silk: 4, spice: 1, amber: 0 } }),
    act("beta", "offer", { keep: { silk: 1, spice: 2, amber: 3 } }),
    act("alpha", "accept", {}, { result: {
      winner: "alpha", outcome_type: "won", termination: "deal", points: { alpha: 15, beta: 14 }, walk_aways: { alpha: 2, beta: 3 },
      claims: { alpha: { claimed: { silk: 5, spice: 1, amber: 0 }, actual: { silk: 5, spice: 1, amber: 0 }, matches: true } },
    } }),
  ],
};

/** Dealt blotto: public field weights, one round, a tied field. */
export const BLOTTO_DEALT = {
  session: session("emporia:blotto:v2", {
    battlefields: 4, troops: 40, rounds: 1, round: 1, phase: "reveal", wins: { alpha: 1 }, weights: [3, 1, 5, 2],
    history: [{ round: 1, allocations: { alpha: [20, 10, 5, 5], beta: [10, 10, 20, 0] }, winner: "alpha" }],
  }),
  actions: [
    act("alpha", "commit", { commitment: H }), act("beta", "commit", { commitment: H }),
    act("alpha", "reveal", { move: [20, 10, 5, 5], nonce: "n1" }),
    act("beta", "reveal", { move: [10, 10, 20, 0], nonce: "n2" }, { result: { winner: "alpha", outcome_type: "win" } }),
  ],
};

/** Two-thirds where both players picked the same number: a draw. */
export const TWO_THIRDS_TIE = {
  session: session("emporia:two-thirds:v2", { phase: "reveal", guesses: { alpha: 40, beta: 40 }, commitments: { alpha: H, beta: H } }),
  actions: [
    act("alpha", "commit", { commitment: H }), act("beta", "commit", { commitment: H }),
    act("alpha", "reveal", { move: 40, nonce: "n1" }),
    act("beta", "reveal", { move: 40, nonce: "n2" }, { result: { winner: null, outcome_type: "draw" } }),
  ],
};

export const LOWEST_UNIQUE = {
  session: session("emporia:lowest-unique:v2", { phase: "reveal", max_pick: 8, guesses: { alpha: 2, beta: 5 }, commitments: { alpha: H, beta: H } }),
  actions: [
    act("alpha", "commit", { commitment: H }), act("beta", "commit", { commitment: H }),
    act("alpha", "reveal", { move: 2, nonce: "n1" }),
    act("beta", "reveal", { move: 5, nonce: "n2" }, { result: { winner: "alpha", outcome_type: "win" } }),
  ],
};

/** Lowest unique where the two picks collide: nobody picked a number alone. */
export const LOWEST_UNIQUE_TIE = {
  session: session("emporia:lowest-unique:v2", { phase: "reveal", max_pick: 8, guesses: { alpha: 3, beta: 3 }, commitments: { alpha: H, beta: H } }),
  actions: [
    act("alpha", "commit", { commitment: H }), act("beta", "commit", { commitment: H }),
    act("alpha", "reveal", { move: 3, nonce: "n1" }),
    act("beta", "reveal", { move: 3, nonce: "n2" }, { result: { winner: null, outcome_type: "draw" } }),
  ],
};

/** Spillway (section 2.5 of the games plan): the dealt opening, the drops with the gate shut for each. */
const spillwayData = (moves: unknown[], extra: Record<string, unknown> = {}) => ({
  cols: 6, rows: 5, drops: 4, move_time_limit_s: 600, seats: ["alpha", "beta"],
  opening: {
    silt: [[1, 1], [6, 1]],
    stones: { alpha: [[2, 1], [4, 1], [3, 2]], beta: [[3, 1], [5, 1], [2, 2]] },
    first: "alpha", attempt: 1,
  },
  moves, ended: null, voided: null, rand: { round: 999, reveals: {} }, ...extra,
});

/** A game won by four in a line: alpha's column, because the gate shut beta's only block. */
export const SPILLWAY = {
  session: session("emporia:spillway:v1", spillwayData([{ n: 1, by: "alpha", channel: 4, row: 2, gate: 6, round: 1000, at: "2026-11-13T12:00:00Z" },
    { n: 2, by: "beta", channel: 5, row: 2, gate: 1, round: 1001, at: "2026-11-13T12:01:00Z" },
    { n: 3, by: "alpha", channel: 4, row: 3, gate: 2, round: 1002, at: "2026-11-13T12:02:00Z" },
    { n: 4, by: "beta", channel: 1, row: 2, gate: 4, round: 1003, at: "2026-11-13T12:03:00Z" },
    { n: 5, by: "alpha", channel: 4, row: 4, gate: 6, round: 1004, at: "2026-11-13T12:04:00Z" }])),
  actions: [
    act("alpha", "drop", { channel: 4, round: 1000 }),
    act("beta", "drop", { channel: 5, round: 1001 }),
    act("alpha", "drop", { channel: 4, round: 1002 }),
    act("beta", "drop", { channel: 1, round: 1003 }),
    act("alpha", "drop", { channel: 4, round: 1004 }, { result: { winner: "alpha", outcome_type: "won", termination: "four", line: [[4, 1], [4, 2], [4, 3], [4, 4]], threes: { alpha: 0, beta: 0 }, points: { alpha: 1, beta: -1 }, moves: 5 } }),
  ],
};

/** The drops run out: alpha has two live threes to beta's one. */
export const SPILLWAY_THREES = {
  session: session("emporia:spillway:v1", spillwayData([{ n: 1, by: "alpha", channel: 2, row: 3, gate: 1, round: 1000, at: "2026-11-13T12:00:00Z" },
    { n: 2, by: "beta", channel: 4, row: 2, gate: 1, round: 1001, at: "2026-11-13T12:01:00Z" },
    { n: 3, by: "alpha", channel: 4, row: 3, gate: 2, round: 1002, at: "2026-11-13T12:02:00Z" },
    { n: 4, by: "beta", channel: 6, row: 2, gate: 3, round: 1003, at: "2026-11-13T12:03:00Z" },
    { n: 5, by: "alpha", channel: 6, row: 3, gate: 2, round: 1004, at: "2026-11-13T12:04:00Z" },
    { n: 6, by: "beta", channel: 6, row: 4, gate: 1, round: 1005, at: "2026-11-13T12:05:00Z" },
    { n: 7, by: "alpha", channel: 2, row: 4, gate: 6, round: 1006, at: "2026-11-13T12:06:00Z" },
    { n: 8, by: "beta", channel: 5, row: 2, gate: 4, round: 1007, at: "2026-11-13T12:07:00Z" }])),
  actions: [
    act("alpha", "drop", { channel: 2, round: 1000 }),
    act("beta", "drop", { channel: 4, round: 1001 }),
    act("alpha", "drop", { channel: 4, round: 1002 }),
    act("beta", "drop", { channel: 6, round: 1003 }),
    act("alpha", "drop", { channel: 6, round: 1004 }),
    act("beta", "drop", { channel: 6, round: 1005 }),
    act("alpha", "drop", { channel: 2, round: 1006 }),
    act("beta", "drop", { channel: 5, round: 1007 }, { result: { winner: "alpha", outcome_type: "won", termination: "threes", threes: { alpha: 2, beta: 1 }, points: { alpha: 1, beta: -1 }, moves: 8 } }),
  ],
};

/** The drops run out level on live threes: the player who moved second (beta) wins. */
export const SPILLWAY_LEVEL = {
  session: session("emporia:spillway:v1", spillwayData([{ n: 1, by: "alpha", channel: 2, row: 3, gate: 3, round: 1000, at: "2026-11-13T12:00:00Z" },
    { n: 2, by: "beta", channel: 1, row: 2, gate: 4, round: 1001, at: "2026-11-13T12:01:00Z" },
    { n: 3, by: "alpha", channel: 6, row: 2, gate: 1, round: 1002, at: "2026-11-13T12:02:00Z" },
    { n: 4, by: "beta", channel: 4, row: 2, gate: 1, round: 1003, at: "2026-11-13T12:03:00Z" },
    { n: 5, by: "alpha", channel: 1, row: 3, gate: 5, round: 1004, at: "2026-11-13T12:04:00Z" },
    { n: 6, by: "beta", channel: 2, row: 4, gate: 5, round: 1005, at: "2026-11-13T12:05:00Z" },
    { n: 7, by: "alpha", channel: 2, row: 5, gate: 1, round: 1006, at: "2026-11-13T12:06:00Z" },
    { n: 8, by: "beta", channel: 6, row: 3, gate: 5, round: 1007, at: "2026-11-13T12:07:00Z" }])),
  actions: [
    act("alpha", "drop", { channel: 2, round: 1000 }),
    act("beta", "drop", { channel: 1, round: 1001 }),
    act("alpha", "drop", { channel: 6, round: 1002 }),
    act("beta", "drop", { channel: 4, round: 1003 }),
    act("alpha", "drop", { channel: 1, round: 1004 }),
    act("beta", "drop", { channel: 2, round: 1005 }),
    act("alpha", "drop", { channel: 2, round: 1006 }),
    act("beta", "drop", { channel: 6, round: 1007 }, { result: { winner: "beta", outcome_type: "won", termination: "level", threes: { alpha: 1, beta: 1 }, points: { alpha: -1, beta: 1 }, moves: 8 } }),
  ],
};

/** Before the beacon's round is published: no opening yet. */
export const SPILLWAY_PRE = {
  session: session("emporia:spillway:v1", { cols: 6, rows: 5, drops: 4, seats: ["alpha", "beta"], opening: null, moves: [], ended: null, rand: { round: 4242, reveals: {} } }, "active"),
  actions: [] as SessionAction[],
};
