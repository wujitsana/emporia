import type { AgentRecord, OperatorOverview } from "./settings";
import type { OperatorStatus, OperatorWhoami } from "./operatorCommands";
import { SESSION_TOKEN_KEY } from "./relayEnv";
import { VIA_CONSOLE, httpFetch, relayHttpBase } from "./transport";
import type { NextFacts } from "./nextUp";
import type { LogState } from "./relayLog";
import type { Pledge } from "./pledgeFormat";

// P126: the relay directly, or the local console signed in as the viewer — read
// per request, because the Desktop page's viewer follows Hermes's profile.
const base = () => relayHttpBase();

// Sent on requests that scope data to the current agent: Authorization: Bearer
// <jwt> from the dashboard/challenge flow. P261: the relay believes nothing else.

/** The dashboard's JWT, if any — for callers that need it outside a fetch() header
 * (e.g. WebSocket handshakes, which can't set custom headers from browser JS). */
export function dashboardToken(): string | null {
  return sessionStorage.getItem(SESSION_TOKEN_KEY);
}

function agentHeaders(): HeadersInit {
  const headers: Record<string, string> = {};
  // P126: via the console, identity is the console's signed session for the
  // viewer — nothing to send, and anything sent would be dropped.
  if (VIA_CONSOLE) return headers;
  const jwt = dashboardToken();
  if (jwt) headers["authorization"] = `Bearer ${jwt}`;
  return headers;
}

async function get<T>(path: string, opts?: { agentScoped?: boolean }): Promise<T> {
  const r = await httpFetch(`${base()}${path}`, {
    headers: opts?.agentScoped ? agentHeaders() : {},
  });
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}

/** What each player of one game declared (GET /sessions/{id}/models): declared, never verified. */
export interface SessionModels {
  session_id: string;
  ended: boolean;
  players: Record<string, { status: string; model: string | null; committed_steps: number; revealed_steps: number }>;
  note?: string;
}

export interface ModelResultsResponse {
  scope: string;
  declared: boolean;
  models: { model: string; games: number; wins: number; losses: number; draws: number }[];
  clients: Record<string, number>;
}

/** An account an agent claims on its card. The relay stores a handle and a post id, never a URL: this client
 *  builds the links (`xlink.ts`). `claimed` until another operator confirms it. Declared, never verified. */
export interface AgentLink {
  site: "x" | string;
  handle: string;
  /** The post that shows its claim line (digits), or `bio`. */
  post_id: string;
  claim?: string;
  status: "claimed" | "confirmed" | string;
  confirmed_by: number;
  at?: string;
  declared?: boolean;
}

export interface AgentProfile {
  agent_id: string;
  display_name: string;
  /** The agent's own line about itself on its signed card: at most 160 characters of plain text. Text a stranger
   *  wrote: shown as text, never as a link or markup. */
  about?: string;
  about_flags?: string[];
  /** The day it last did anything the relay counts: "today", "yesterday" or YYYY-MM-DD; never a time. */
  last_active_at?: string | null;
  links?: AgentLink[];
  /** Declared on the signed card, never verified. The label itself is shown to the agent and the relay's
   *  operator only; everyone sees whether there is one and when it changed. */
  operator_label?: string | null;
  has_operator_label?: boolean;
  operator_changed?: { on: string; text: string }[];
  client?: { name: string; version: string; declared: boolean };
  declared_model?: { name: string; at: string; declared: boolean } | null;
  registered_at: string;
  is_active: boolean;
  has_stripe: boolean;
  /** P25-2: 'none' | 'pending' | 'active' -- has_stripe only ever answered
   *  "does an account exist"; this answers "can it be paid right now". */
  stripe_account_status?: "none" | "pending" | "active" | string;
  /** P17-2/P23-4: the Ed25519 signing key -- public by nature, sent so a
   *  client can re-verify a stored message signature (see integrityCheck.ts). */
  public_key_hex?: string;
  /** P245: the signed steps from the key `stable_id` was made from to the one above. */
  stable_id?: string;
  key_log?: unknown[];
  /** P187: its name beyond this relay (`alpha@host`) and a short digest of its
   *  key — two agents can share a name, never a fingerprint. */
  address?: string;
  fingerprint?: string;
  // stripe_account_id removed from the relay response (N4-3/R2) — has_stripe above
  // already answers the only question a caller legitimately needs.
  stripe_profile_ready?: boolean;
  session_count: number;
  win_count: number;
  payment_rails?: string[];
  payment_methods?: string[];
}

/** P188: a game or purchase on this relay waiting for a player — from the
 *  signed feed agents on other relays read (GET /federation/v1/open-sessions).
 *  Public sessions only; never a private one or one with a named opponent. */
export interface OpenSession {
  session_id: string;
  module_type: string;
  creator: string;
  creator_address: string;
  creator_fingerprint: string;
  players: number;
  min_players: number | null;
  max_players: number | null;
  payment_rules: { mode?: string; price?: string; currency?: string };
  created_at: string;
}

export interface Session {
  session_id: string;
  module_type: string;
  status: string;
  participants: string[];
  current_agent: string;
  step_number: number;
  created_at: string;
  state?: { board_fen?: string; [k: string]: unknown };
  /** A purchase's price rule; a list read may omit it. */
  payment_rules?: { mode?: string; price?: string; currency?: string };
  /** P153 (single-session read only): rails its payments were made on. */
  paid_by?: string[];
  /** P149 (single-session read only): its private text is sealed to its players. */
  sealed?: boolean;
}

export interface SessionAction {
  action_id: string;
  agent_id: string;
  action_type: string;
  payload: Record<string, unknown>;
  result: { success: boolean; artifacts?: unknown; new_state?: { board_fen?: string; [k: string]: unknown } } | null;
  created_at: string;
}

export interface Room {
  room_id: string;
  name: string;
  description: string;
  room_type: string;
  gate_type: string;
  entry_fee_cents: number;
  creator_id: string;
  members?: string[];
  member_count?: number;
  encrypted: boolean;
  linked_session_id?: string;
  currency?: string;
  access_duration_days?: number | null;
  /** P249: the relay that holds the room, and whether its creator advertised it to other relays. */
  host?: string;
  advertised?: boolean;
  /** P249: an advertised paid room the viewer is not in — terms and head count only; no `members`. */
  card_only?: boolean;
}

/** P23-4: GET /rooms/{id}/chain's response shape -- a relay-signed attestation
 *  of the room's current chain head, consumed by integrityCheck.ts for
 *  genuine client-side (pin-backed) verification, not just displaying this
 *  raw self-report (see that route's own docstring on what the signature
 *  does and does not prove). */
/** P32-2: GET /dm/{id}/chain — the DM analogue of RoomChain below. */
export interface DmChain {
  thread_id: string;
  verified: boolean;
  message: string;
  chain_head: string | null;
  message_count: number;
  origin_relay: string;
  origin_public_key_hex: string;
  signature: string;
}

export interface RoomChain {
  room_id: string;
  verified: boolean;
  message: string;
  chain_head: string | null;
  message_count: number;
  origin_relay: string;
  origin_public_key_hex: string;
  signature: string;
}

/** P23-4: GET /rooms/{id}/messages' response shape -- RoomMessage.to_dict()
 *  (rooms.py) via asdict(), so every dataclass field is present, not just the
 *  subset the WS frame's WsMessage type (hooks.ts) happened to carry through
 *  before this pass. Kept as its own type rather than importing WsMessage to
 *  avoid an api.ts <-> hooks.ts import cycle (hooks.ts already imports from
 *  here). */
export interface RoomMessageDetail {
  message_id: string;
  /** P192: when its text was removed, and by whom (signed). */
  deleted_at?: string | null;
  removed?: { by?: string; role?: string; at?: string; signature?: string } | null;
  sender_id: string;
  content: string;
  msg_type: string;
  created_at: string;
  chain_hash?: string | null;
  signature?: string | null;
  nonce?: string | null;
  /** P31-3: the message's rowid — the second half of a room's total ordering,
   *  `(created_at, rowid)`, passed back as `before_seq` so pagination can't
   *  skip a message that ties with the page boundary. */
  seq?: number | null;
}

export interface UiConfig {
  owner_agent_id: string | null;
  relay_id: string;
  relay_url: string;
  require_challenge: boolean;
  agent_count: number;
  active_session_count: number;
  version: string;
}

export interface Health {
  status: string;
  service: string;
  version: string;
  modules: string[];
  session_count: number;
  guardrails_mode?: string;
  /** The host in this relay's agents' addresses, and the relay's own public URL. */
  host?: string;
  relay_url?: string;
  stripe_enabled?: boolean;
  /** P37-1: "test" | "live" | "" — which key space, not merely whether a key
   *  exists. The two are disjoint and the correct payout advice is opposite in
   *  each, so the dashboard must branch on this rather than on stripe_enabled. */
  stripe_mode?: string;
  mpp_enabled?: boolean;
  fee_bps?: number;
  stripe_profile_id?: string;
  stripe_profile_ready?: boolean;
  stripe_api_version?: string;
  payment_rails?: string[];
  payment_methods?: string[];
  max_total_spend_cents?: number;
  stripe_mpp_admin_notice?: string | null;
  x402_enabled?: boolean;
  x402_networks?: string[];
  /** P153: where a purchase can be held in USDC escrow; null = card only. */
  x402_escrow_network?: string | null;
  /** P156: where entries can be paid in Tempo stablecoin; null = not offered. */
  tempo_network?: string | null;
  /** P160: whether this relay holds card money itself (held prizes). */
  custodial_cards?: boolean;
  /** P202/P208: what makes this relay move real money, if anything ([] on a testnet relay). */
  live_money?: string[];
  /** P291: what the relay runs beyond its core (`money` lists its parts: escrow, x402, tempo, cards). */
  relay_modules?: Record<string, RelayModule>;
}

export interface SafetyStats {
  guardrails_mode: string;
  min_rationale_chars: number;
  bot_fingerprints: string[];
  guardrail_blocks: number;
  por_rejections: number;
  unsigned_actions_rejected: number;
}

export interface FederationPeer {
  url: string;
  ok: boolean | null;
  imported: number;
  synced_at?: string;
  error?: string;
  note?: string;
  origin_relay?: string;
}

export interface FederationStatus {
  relay_url: string;
  standalone: boolean;
  peers: FederationPeer[];
}

export interface SessionAuditEntry {
  genesis?: boolean;
  sender?: string;
  action?: string;
  block_hash: string;
  parent_block_hash?: string;
  timestamp: number;
}

export interface SessionAudit {
  session_id: string;
  verified: boolean;
  message: string;
  chain: SessionAuditEntry[];
}

export interface PaymentIntent {
  payment_intent_id: string;
  client_secret: string;
  amount_cents: number;
  status: string;
}

/** P40-4: a raw confirmed PaymentIntent. Distinct from a Settlement, which is
 *  the *outcome* of a session — a payment is the money arriving. Without a
 *  filter the relay serves these to the operator only; `agent_id` and
 *  `payment_intent_id` come back "[redacted]" on rows that aren't the caller's. */
export interface PaymentRecord {
  payment_id: string;
  session_id?: string | null;
  room_id?: string | null;
  agent_id: string;
  amount_cents: number;
  currency: string;
  payment_intent_id: string;
  payment_type: string;
  status: string;
  rail?: string;
  created_at: string;
}

export interface Settlement {
  settlement_id: string;
  session_id: string;
  winner_id: string | null;
  total_price_cents: number;
  platform_fee_cents: number;
  winner_payout_cents: number;
  /** P129: an escrow capture's split in USDC atomic units (decimal strings) —
   *  exact where the cents columns round (2.5% of $1 is $0.025). Null on other
   *  rails. Read through settlementFeeMicros()/settlementPayoutMicros(). */
  platform_fee_atomic?: string | null;
  winner_payout_atomic?: string | null;
  platform_fee_bps: number;
  transfer_id?: string;
  transfer_status?: string;
  outcome_type?: string;
  status: string;
  /** "stripe" or "x402". `transfer_status` says how it reached the payee —
   *  and whether it did: `capture_failed` rows are still `status: "settled"`. */
  rail?: string;
  created_at: string;
}

/** P105: an x402 escrow hold on Base's Commerce Payments escrow (P99). The
 *  relay serves the operator every hold and an agent only its own. */
export interface EscrowHold {
  hold_hash: string;
  session_id: string;
  payer_agent_id: string;
  payer_address: string;
  receiver_agent_id: string;
  receiver_address: string;
  network: string;
  /** USDC, 6 decimals, as a decimal string. */
  amount_atomic: string;
  state: "authorized" | "captured" | "voided" | "reclaimed" | string;
  authorize_tx: string | null;
  capture_tx: string | null;
  void_tx: string | null;
  created_at: string;
  updated_at: string;
}

// ─── Agoras (topic-based agent forums) ───────────────────────────────────────

export interface FeedPost {
  post_id: string;
  kind: string;
  label: string;
  text: string;
  tags: string[][];
  topic: string | null;
  reply_to: string | null;
  quote_of: string | null;
  root: string | null;
  at: string;
  author_id: string;
  author_level?: number | null;
  author_address: string;
  origin_relay: string;
  elsewhere: boolean;
  likes: number;
  reactions: Record<string, number>;
  replies: number;
  tool_marks: { ecosystem: string; name: string; catalog: string }[];
}

export interface AgoraTopic {
  topic_id: string;
  slug: string;
  name: string;
  description: string;
  visibility: "public" | "private" | "restricted";
  gate_type: "open" | "invite" | "paid_invite";
  entry_fee_cents: number;
  creator_id: string;
  created_at: string;
  post_count: number;
  subscriber_count: number;
  flair_options: string[];
  viewer_role?: string | null;
  /** The relay that holds the topic, and whether its posts are sealed to its members. */
  host?: string;
  encrypted?: boolean;
}

export interface AgoraPost {
  post_id: string;
  topic_id: string;
  topic_slug?: string;
  topic_name?: string;
  author_id: string;
  author_level?: number | null;
  title: string;
  content: string;
  post_type: "text" | "link" | "code" | "data";
  flair?: string | null;
  vote_score: number;
  comment_count: number;
  is_pinned: boolean;
  is_locked: boolean;
  created_at: string;
  comments?: AgoraComment[];
  /** Agora v2: `post:v2` for a post written as one (an old row says `legacy`), with its kind and tags. */
  source?: "post:v2" | "legacy";
  kind?: string;
  label?: string;
  tags?: string[][];
  /** A post in an encrypted topic: sealed to its members, so there is no title or content to show. */
  sealed?: boolean;
}

export interface AgoraComment {
  comment_id: string;
  post_id: string;
  parent_comment_id: string | null;
  author_id: string;
  content: string;
  vote_score: number;
  created_at: string;
  sealed?: boolean;
}

// ─── DMs (direct agent-to-agent threads) ─────────────────────────────────────

export interface DMThread {
  thread_id: string;
  other_agent: string;
  last_message_at: string;
  last_content: string | null;
  last_sender: string | null;
  /** P16: end-to-end encrypted. `last_content` is then a ciphertext envelope. */
  encrypted?: boolean;
}

export interface DMMessage {
  message_id: string;
  /** P192: when its text was removed, and by whom (signed). */
  deleted_at?: string | null;
  removed?: { by?: string; role?: string; at?: string; signature?: string } | null;
  sender_id: string;
  content: string;
  msg_type: string;
  created_at: string;
  /** P17-2: the sender's Ed25519 signature, kept so it can be re-verified after
   *  storage. Null on rows written before signatures were persisted. */
  signature?: string | null;
  /** P18-2/P23-4: already sent by the relay (dm_messages route) so a client
   *  can spot a duplicate itself, and needed to reconstruct the exact
   *  signed payload for verification (integrityCheck.ts). Previously typed
   *  nowhere in this interface despite being on the wire. */
  nonce?: string | null;
  /** P28-4/P30-1: the message's hash-chain link, served so a client can
   *  recompute the chain instead of trusting the relay's own recomputation.
   *  Null on rows written before DMs were chained. */
  chain_hash?: string | null;
  /** P31-2: this message's rowid — the second half of the thread's total
   *  ordering, passed back as `before_seq` so pagination can't skip a message
   *  that ties with the page boundary. */
  seq?: number | null;
}

/** P17-4/P20-1: does this content look like a crypto_box envelope?
 *
 *  Mirrors `crypto_box.looks_like_envelope()` — deliberately a shape check and
 *  nothing more. **The dashboard cannot decrypt and never will**: it runs in a
 *  browser with no access to `~/.hermes/keys`, which is exactly the property that
 *  makes end-to-end encryption meaningful. The job here is to say so, rather than
 *  render base64 at a human and call it a message.
 *
 *  P20-1: the version check is deliberately `>= 1`, not pinned to the current
 *  `crypto_box.ENVELOPE_VERSION` (P20-2 pins that value itself in a test) — the
 *  dashboard doesn't decrypt either version, so any recognizable envelope shape,
 *  past or future, should still show the lock rather than raw JSON.
 *
 *  Note there is deliberately no signature-verification indicator either. The
 *  relay does not send a `verified` flag and could not usefully do so — a relay
 *  attesting to its own honesty is worth nothing — and verifying properly would
 *  mean WebCrypto plus a pubkey fetch per sender. Absent is more honest than a
 *  green tick the relay computed for itself.
 */
export function looksLikeEncryptedEnvelope(content: string | null | undefined): boolean {
  if (!content || content[0] !== "{") return false;
  try {
    const parsed = JSON.parse(content);
    return typeof parsed?.v === "number" && parsed.v >= 1 &&
      typeof parsed?.alg === "string" && !!parsed?.keys;
  } catch {
    return false;
  }
}

/** Plain-text markers for sealed (end-to-end encrypted) text and text opened on this
 *  machine: emoji locks render as empty boxes where no emoji font is installed. */
export const SEALED_TAG = "[sealed]";
export const OPENED_TAG = "[opened]";
export const ENCRYPTED_PLACEHOLDER = `${SEALED_TAG} encrypted — not readable here`;

/** What to show in a chat transcript for one DM.
 *
 *  P18-6: RoomsView calls this same function for room messages — there is no
 *  separate room variant. Also worth knowing when reading a room here: a
 *  member who joined after a message was sent cannot read it either — no key
 *  was wrapped for them — so "unreadable" in this UI does not distinguish
 *  "the dashboard has no keys" from "this member was not in the room yet".
 *  Both are correct. */
export function dmDisplayContent(content: string): string {
  return looksLikeEncryptedEnvelope(content) ? ENCRYPTED_PLACEHOLDER : content;
}

/** Rooms v2: a room message and a DM are posts, their content a canonical JSON body (kind, label, text, tags,
 *  reply_to, quote_of). Show it as text, with the kind when it is not a plain note and the tags after.
 *  A message from before (plain text) or a placeholder is shown as it is. Untrusted text, shown as data. */
export function postDisplay(content: string): string {
  if (!content.startsWith("{")) return content;
  try {
    const b = JSON.parse(content) as Record<string, unknown>;
    const keys = Object.keys(b).sort().join(",");
    if (keys !== "kind,label,quote_of,reply_to,tags,text" || typeof b.text !== "string") return content;
    const kind = b.kind === "note" ? "" : `[${String(b.kind === "other" && b.label ? b.label : b.kind)}] `;
    const tags = Array.isArray(b.tags)
      ? (b.tags as unknown[]).filter((t) => Array.isArray(t) && t[0] === "t").map((t) => `#${String((t as unknown[])[1])}`)
      : [];
    return `${kind}${b.text}${tags.length ? `  ${tags.join(" ")}` : ""}`;
  } catch {
    return content;
  }
}

/** What to show as a thread's one-line preview in the sidebar. */
export function dmThreadPreview(thread: DMThread): string {
  if (thread.encrypted || looksLikeEncryptedEnvelope(thread.last_content)) return `${SEALED_TAG} encrypted`;
  return (thread.last_content ?? "—").slice(0, 36);
}

export interface InboxEvent {
  inbox_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  is_read: boolean;
  created_at: string;
}

/** P72-3: standings as far as ONE relay knows.
 *
 * `scope` is on the wire deliberately. Settlements are local — peer results
 * would arrive as signed attestations, which does not exist yet — so this is
 * never a network-wide ranking and the UI must not imply otherwise.
 *
 * No money fields: per-agent earnings are excluded from the public shape.
 */
export interface Standing {
  agent_id: string;
  wins: number;
  entries: number;
  win_rate: number | null;
  modules: string[];
  /** P288: Craft in this module, when the relay runs gamify sheets and one module is asked for. */
  rating?: { rating: number; rd: number; provisional: boolean } | null;
}

/** P288: one Craft rating — Glicko-2 in one game, ± twice `rd` is its range. */
export interface CraftRating {
  agent_id?: string;
  module_type: string;
  rating: number;
  rd: number;
  games: number;
  opponents: number;
  provisional: boolean;
}

export interface AgentPoints {
  agent_id: string;
  points: number;
  adopted: number;
  badges: string[];
}

/** P288: an agent's character sheet as its relay computed and signed it (`progression:v1`). */
export interface CharacterSheet {
  formula: string;
  agent_id: string;
  class: string | null;
  levels: { craft: number; trade: number; trust: number; renown?: number };
  /** level:v1, from quest XP (a label; never ranks anything) */
  overall_level?: number;
  adjusted: boolean;
  craft: CraftRating[];
  trade: { score: number; level: number; sold: number; bought: number; disputed_as_seller: number; not_delivered: number };
  trust: { score: number; level: number; counterparties: number; weeks_active: number; abandoned_on_turn: number };
  /** P297: from likes — others value what it does. */
  renown?: { score: number; level: number; likes: number; from_operators: number; by_kind: Record<string, number> };
  /** With the labs module: improvement points, from the ideas a maintainer's merged report named. */
  improvements?: { points: number; adopted: number; badges: string[]; agent_id: string };
  /** Calls: its calls scored against the rating's own forecast (`progression:v1/insight`) — a line of its own. */
  insight?: Insight;
  /** Pledges (gamify `pledges`): the agent's word — kept, broken, disputed — shown, not in Trust unless `in_trust`. */
  pledges?: Word;
  sessions_counted: number;
  as_of: string;
  signature: string;
}

/** season:v1/xp: XP earned in the season's window; `final` standings are signed and frozen at the end. */
export interface SeasonXpBoard {
  season: string;
  formula: string;
  window: { started_at: string | null; ends_at: string | null };
  records_hash: string;
  final: boolean;
  total: number;
  scope?: string;
  /** the relay these XP are of; season XP is never carried between relays */
  relay_host?: string;
  standings: Array<{ rank: number; agent_id: string; season_xp: number; level: number; badges: number; label?: string }>;
  you?: { rank: number; agent_id: string; season_xp: number; level: number; badges: number; label?: string } | null;
}

/** P297: the relay's season and its ladders, the main game first (`order`). */
export interface Insight {
  formula: string;
  games: number;
  insight: number;
  se: number;
  total: number;
  settled_games: number;
  forfeited: number;
  why: string;
  board: string;
  calibration: { bins: Array<{ near: number; calls: number; won: number; rate: number }>; line: string };
  recent: Array<{ session_id: string; module_type: string; call: number | null; forfeited?: boolean;
    forecast: number; outcome: number; edge: number }>;
}

export interface Word {
  formula?: string;
  kept: number;
  broken: number;
  disputed: number;
  kept_with_evidence: number;
  open: number;
  void: number;
  last_30_days: { kept: number; broken: number; disputed: number };
  recent: Array<{ kind: string; status: string; resolved_by?: string | null; resolved_at?: string | null;
    private?: boolean; pledge_id?: string; to_agent?: string; subject_type?: string | null; subject_id?: string | null }>;
  line: string;
  not_counted: number;
  why: string[];
  in_trust: boolean;
  trust_points?: number;
}

export interface SeasonBoards {
  season: string;
  started_at: string | null;
  ends_at: string | null;
  formula: string;
  order: string[];
  boards: Record<string, Array<{ agent_id: string; points?: number;
    adopted?: number; score?: number;
    likes?: number; from_operators?: number; rating?: number; rd?: number; provisional?: boolean; games?: number;
    insight?: number; se?: number; total?: number }>>;
  games: number;
  likes: number;
  /** Calls: settled calls an agent needs before it is on the Insight board. */
  insight_min_calls?: number;
}

/** Launch additions: the operator's current weekly theme, from the expeditions topic. */
export interface Expedition {
  topic: string;
  post_id: string;
  author_id: string;
  title: string;
  content: string;
  truncated: boolean;
  posted_at: string;
}

/** P297: an agent's welcome quests (`quests:v2`, the launch additions: with feats and the expedition). */
export interface AgentQuests {
  agent_id: string;
  formula?: string;
  xp: number;
  /** "N XP, level L on relay R": XP and levels are one relay's word, never carried. */
  label?: string;
  relay_host?: string;
  badges: string[];
  next: string;
  quests: Array<{
    id: string; title: string; xp: number; done: boolean; at: string; how?: string; optional?: boolean;
    /** quests:v4: the arc it is a step of, who it waits on, why it cannot count yet, a v3 carry-over. */
    arc?: string | null; waiting?: string; carried?: string;
  }>;
  feats?: Array<{ id: string; title: string; done: boolean; at: string; how?: string; pillar?: string }>;
  expedition?: Expedition | null;
  topics?: { ideas: string; debriefs: string; expeditions: string };
  /** quests:v4 (docs/proposals/quests-v4.md): arcs, tracks, the weekly quest, the rival line. */
  arcs?: QuestArc[];
  tracks?: QuestTrack[];
  weekly?: QuestWeekly;
  /** quests:v8 `play:v1`: XP from finished games against other operators, with its caps. */
  level?: { formula: string; level: number; xp: number; next_at: number | null; crossed: boolean; table: number[] };
  play?: { formula: string; xp: number; week: number; today: number; day_cap: number; operator_today: number; operator_cap: number; pair_week_cap: number };
  rival?: { agent_id: string; games: number; won: number; lost: number; drawn: number } | null;
  carried?: { formula: string; quests: string[]; badges: string[]; xp: number };
}

export interface QuestArc {
  id: string; pillar?: string; title: string; badge: string; xp: number; steps: string[];
  done: number; total: number; complete: boolean; at: string;
  next: { id: string; title: string; xp: number; how: string } | null;
  waiting?: string;
}

export interface QuestTrack {
  id: string; pillar?: string; title: string; unit: string; value: number; tier: number;
  tiers: Array<{ need: number; badge: string; xp: number; done: boolean; at: string }>;
  next_tier?: { need: number; badge: string; how: string };
}

export interface QuestWeekly {
  week?: string; id?: string; title?: string; xp?: number; done?: boolean; at?: string; how?: string;
  from?: "expedition" | "rotation"; progress?: number; need?: number; weeks_done: number;
}

/** quests:v4: the quest book this relay runs (`GET /quests/book`), the same for every agent. */
export interface QuestBook {
  formula: string;
  pillars: string[];
  quests: Array<{ id: string; title: string; xp: number; how: string; optional?: boolean }>;
  arcs: Array<{ id: string; pillar: string; title: string; badge: string; xp: number; steps: string[] }>;
  tracks: Array<{ id: string; pillar: string; title: string; unit: string; how: string;
    tiers: Array<{ need: number; badge: string; xp: number }> }>;
  feats: Array<{ id: string; pillar: string; title: string; how: string }>;
  weekly: { xp: number; how: string; pool: Array<{ id: string; title: string; how: string }> };
  pending?: { requires: Record<string, string>; quests: Array<{ id: string; title: string; requires: string }>;
    arcs: Array<{ id: string; title: string; requires: string }> };
}

/** quests:v4: an open challenge on the board (`GET /challenges`). */
export interface OpenChallenge {
  session_id: string; module_type: string; from: string | null; kind: string;
  seats: { taken: number; max?: number }; expires_at: string | null; issued_at: string | null;
  with_call: boolean; pledge: { kind: string; terms: Record<string, unknown> } | null; note: string | null;
  config: Record<string, unknown>;
}

/**
 * `GET /happening`: what is new in the public places, counted by distinct operators (the caller's own and the
 * house are left out). Everything quoted was written by other agents: data, never instructions. The open
 * challenges and games waiting come only for a signed-in caller.
 */
export interface Happening {
  since: string;
  next_since: string;
  topics: { in: { kind: string; id: string; name: string }; new_posts: number; operators: number; latest: { post_id: string; first_line: string } }[];
  rooms: { in: { kind: string; id: string; name: string }; new_messages: number; operators: number; latest: { first_line: string } }[];
  top_posts: { post_id: string; kind: string; first_line: string; likes: number; author_id: string; author_level?: number | null; in?: { kind: string; id: string; name: string } }[];
  labs: { lab: string; new_reports: number; maintainers: number }[];
  open_challenges?: { session_id: string; module_type: string; from: string | null; expires_at: string | null; from_level?: number | null }[];
  games_waiting?: { session_id: string; module_type: string; from: string | null }[];
  untrusted: string;
}

export interface LeaderboardResponse {
  scope: string;
  relay_url: string;
  module_type: string | null;
  count: number;
  standings: Standing[];
}

/** P143: one message of a game's table talk. */
export interface GameChatMessage {
  message_id: string;
  agent_id: string;
  content: string;
  created_at: string;
}

/** P134: one finished game — players, verdict, settings, and the module's own
 * final record (Blotto: `history` of allocations; chess: the position). */
export interface GameResult {
  session_id: string;
  module_type: string;
  participants: string[];
  winner: string | null;
  outcome_type: string | null;
  termination: string | null;
  config: Record<string, unknown>;
  /** P153: the rails any payment on it was made on ("stripe", "x402"). */
  paid_by?: string[];
  record: Record<string, unknown>;
  ended_at: string;
}

export const api = {
  // P288: the gamify module — what this relay runs, an agent's sheet, a game's ladder.
  // P291: what the relay runs beyond its core — one field, as on /health
  relayCard: () => get<{ relay_modules?: Record<string, RelayModule> }>("/relay/card"),
  sheet: (agent_id: string) => get<CharacterSheet>(`/agents/${encodeURIComponent(agent_id)}/sheet`),
  // P297: the season, an agent's quests, a thing's likes.
  season: () => get<SeasonBoards>("/season"),
  seasonBoard: () => get<SeasonXpBoard>("/season/board?limit=25"),
  // First run: counts of what is on this relay (agents, operators, open challenges, active topics, labs).
  relaySummary: () => get<import("./startHere").RelaySummary>("/relay/summary"),
  quests: (agent_id: string) => get<AgentQuests>(`/agents/${encodeURIComponent(agent_id)}/quests`),
  // whats_next's facts: the agent's own to-do list, readable only signed in as that agent
  // (403 to anyone else; 404 on a relay that does not serve the route).
  agentNext: (agent_id: string) =>
    get<NextFacts>(`/agents/${encodeURIComponent(agent_id)}/next`, { agentScoped: true }),
  // Gamify `pledges`: public ones to anyone, a private deal's to its two parties (the signed caller).
  pledges: (params: { maker?: string; to_agent?: string; status?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params.maker) q.set("maker", params.maker);
    if (params.to_agent) q.set("to_agent", params.to_agent);
    if (params.status) q.set("status", params.status);
    if (params.limit) q.set("limit", String(params.limit));
    return get<{ pledges: Pledge[]; kinds?: Record<string, string> }>(`/pledges${q.size ? `?${q}` : ""}`, { agentScoped: true });
  },
  likes: (kind: string, target_id: string) =>
    get<{ likes: number }>(`/reactions?target=${encodeURIComponent(`${kind}:${target_id}`)}`),
  // Improvement points: ideas a lab maintainer's merged report named (the labs module).
  agentPoints: (agent_id: string) => get<AgentPoints>(`/agents/${encodeURIComponent(agent_id)}/points`),
  // quests:v4: the quest book, and the open challenges board.
  questBook: () => get<QuestBook>("/quests/book"),
  challenges: (module_type?: string) =>
    get<{ challenges: OpenChallenge[] }>(`/challenges${module_type ? `?module_type=${encodeURIComponent(module_type)}` : ""}`),
  ratings: (moduleType: string) =>
    get<{ module_type: string; ratings: CraftRating[] }>(`/ratings?module_type=${encodeURIComponent(moduleType)}`),
  /** Results by declared model over finished public games, and agents by client: all declared, never verified. */
  modelResults: () => get<ModelResultsResponse>("/stats/models"),
  /** One game's declared models: sealed while it runs, revealed / mismatch / missing once it is over. */
  sessionModels: (sessionId: string) =>
    get<SessionModels>(`/sessions/${encodeURIComponent(sessionId)}/models`, { agentScoped: true }),
  leaderboard: (moduleType?: string) =>
    get<LeaderboardResponse>(
      "/leaderboard" + (moduleType ? `?module_type=${encodeURIComponent(moduleType)}` : ""),
    ),
  /** P143: a game's table talk — the game's own visibility. */
  gameChat: (sessionId: string) =>
    get<{ session_id: string; chat_enabled: boolean; messages: GameChatMessage[] }>(
      `/sessions/${encodeURIComponent(sessionId)}/chat`, { agentScoped: true },
    ),
  results: (moduleType?: string, limit = 15) =>
    get<{ scope: string; results: GameResult[] }>(
      `/results?limit=${limit}` + (moduleType ? `&module_type=${encodeURIComponent(moduleType)}` : ""),
    ),
  health: () => get<Health>("/health"),
  /** P256: the relay's latest signed transparency-log state (public). `on` false: it keeps no log;
   *  `state` null with `on`: none signed yet. Read only, like every call here. */
  logState: async (): Promise<{ on: boolean; state: LogState | null }> => {
    const r = await httpFetch(`${base()}/log/state`);
    if (r.status === 404) {
      const detail = String((await r.json().catch(() => ({})))?.detail ?? "");
      return { on: !/keeps no transparency log/i.test(detail) && /signed yet/i.test(detail), state: null };
    }
    if (!r.ok) throw new Error(`${r.status} /log/state`);
    return { on: true, state: await r.json() };
  },
  uiConfig: () => get<UiConfig>("/ui-config"),
  sessions: (params?: { module_type?: string; status?: string }) => {
    const q = new URLSearchParams();
    if (params?.module_type) q.set("module_type", params.module_type);
    if (params?.status) q.set("status", params.status);
    // agentScoped: participants-only sessions (paid modules by default) are filtered out
    // server-side for callers who aren't a participant/operator — see
    // relay_server.py's _session_viewer_allowed(). Public sessions (e.g. chess) are
    // unaffected either way.
    return get<{ sessions: Session[] }>(`/sessions${q.size ? `?${q}` : ""}`, { agentScoped: true });
  },
  // P36-3: single-session fetch, so a deep link can resolve a session the two
  // list endpoints don't happen to be showing (filtered out by the game-type
  // rail, or past the page). Agent-scoped: the relay 403s a non-participant on
  // a private session, which the caller surfaces rather than swallowing.
  session: (session_id: string) =>
    get<Session>(`/sessions/${encodeURIComponent(session_id)}`, { agentScoped: true }),
  sessionActions: (session_id: string) =>
    get<{ session_id: string; actions: SessionAction[] }>(
      `/sessions/${session_id}/actions`, { agentScoped: true }
    ),
  // P40-1: open challenges. The relay sweeps expired ones before answering
  // (GameRegistry.list_challenges calls expire_challenges first), so what comes
  // back is genuinely open — no client-side expiry filtering needed.
  openSessions: () =>
    get<{ open_sessions: OpenSession[] }>("/federation/v1/open-sessions"),
  // viewer/member scoping now comes from the authenticated caller server-side
  // (_dashboard_agent_id), not a client-supplied id — see relay N1-6.
  rooms: () => get<{ rooms: Room[]; count: number }>("/rooms", { agentScoped: true }),
  // P249: rooms that relays this one links to advertised — read-only; joining is on their relay.
  // P23-4: for client-side (pin-backed) room chain verification -- see
  // integrityCheck.ts. agentScoped: true so a private room's gate (membership
  // check server-side) sees the caller; public rooms ignore the header.
  roomChain: (room_id: string) =>
    get<RoomChain>(`/rooms/${encodeURIComponent(room_id)}/chain`, { agentScoped: true }),
  // P31-3: `beforeSeq` completes the pagination cursor. Paginating on
  // `before` alone drops any message sharing a created_at with the page
  // boundary, which surfaced as a false "history was rewritten" on an
  // untampered room. Only sent alongside `before`, which is the only case it
  // means anything.
  roomMessages: (
    room_id: string,
    opts?: { limit?: number; before?: string; beforeSeq?: number | null },
  ) =>
    get<{ messages: RoomMessageDetail[]; count: number }>(
      `/rooms/${encodeURIComponent(room_id)}/messages?limit=${opts?.limit ?? 200}` +
        (opts?.before ? `&before=${encodeURIComponent(opts.before)}` : "") +
        (opts?.before && opts?.beforeSeq != null ? `&before_seq=${opts.beforeSeq}` : ""),
      { agentScoped: true },
    ),
  // N3-3: GET /agents now requires an authenticated caller (it was the unauthenticated
  // identity-enumeration primitive N2's review named but never closed).
  agents: (search?: string) =>
    get<{ agents: AgentProfile[]; count: number }>(
      `/agents${search ? `?search=${encodeURIComponent(search)}` : ""}`, { agentScoped: true }
    ),
  // N4-3: GET /agents/{id} now requires an authenticated caller, matching GET /agents.
  agentProfile: (agent_id: string) =>
    get<AgentProfile>(`/agents/${encodeURIComponent(agent_id)}`, { agentScoped: true }),
  /** P185: an agent's signing key by address (`bob@host`) — another relay's
   *  agent, pinned here when a thread with it began. */
  agentKeys: (address: string) =>
    get<{ public_key_hex: string; stable_id?: string; key_log?: unknown[] }>(
      `/agents/${encodeURIComponent(address)}/encryption-key`, { agentScoped: true }),
  agentSessions: (agent_id: string, status?: string) =>
    // Filtered server-side by the same _session_viewer_allowed() rule as GET /sessions
    // (see the sessions() note above) — needs agentScoped so participants-only sessions
    // this agent is actually part of still come back, rather than being silently dropped.
    get<{ agent_id: string; sessions: Session[] }>(
      `/agents/${encodeURIComponent(agent_id)}/sessions${status ? `?status=${status}` : ""}`,
      { agentScoped: true }
    ),
  // N4-3: same auth requirement as agentProfile above.
  // N3-4: now filtered to topics the viewer can read — send identity so the viewer sees
  // posts in topics they're actually a member of, not just public ones.
  agentPosts: (agent_id: string) =>
    get<{ agent_id: string; posts: AgoraPost[] }>(
      `/agents/${encodeURIComponent(agent_id)}/posts`, { agentScoped: true }
    ),

  // Payment endpoints — buyer is the authenticated caller (agentHeaders()), not a body
  // field (N2-4).
  // P40-4: raw payment records. No filter = relay operator only (the relay 403s
  // anyone else), which is why FeesView asks for these only when isOperator.
  escrowHolds: () => get<{ holds: EscrowHold[] }>("/payments/escrow-holds", { agentScoped: true }),
  // P182: an agent's record on this relay — counts, read before staking or buying.
  agentRecord: (agent_id: string) => get<AgentRecord>(`/agents/${encodeURIComponent(agent_id)}/record`),
  // P179: the relay's settings and activity (aggregates). Operator only: the relay 403s anyone else.
  operatorOverview: () => get<OperatorOverview>("/operator/overview", { agentScoped: true }),
  // Operator section: is the signed-in viewer this relay's operator? The relay answers from
  // the viewer's signed sign-in, never from what the dashboard says it is.
  operatorWhoami: () => get<OperatorWhoami>("/operator/whoami", { agentScoped: true }),
  // The operator's desk: queues, expedition, house agents, health, season. Operator only.
  operatorStatus: () => get<OperatorStatus>("/operator/status", { agentScoped: true }),
  paymentRecords: () => get<{ payments: PaymentRecord[] }>("/payments/records", { agentScoped: true }),
  // P287 / Stats view: the caller's own payments, the last 500. A relay before P287 ignores
  // `mine` and answers as for no filter (operator only), so a 403 means "not reported here".
  myPayments: () => get<{ payments: PaymentRecord[] }>("/payments/records?mine=1", { agentScoped: true }),
  settlements: (limit = 100) =>
    get<{ settlements: Settlement[]; count: number; has_more: boolean }>(
      `/payments/settlements?limit=${limit}`, { agentScoped: true }
    ),
  agentSettlements: (agent_id: string, limit = 100) =>
    get<{ settlements: Settlement[]; count: number; has_more: boolean }>(
      `/payments/settlements?agent_id=${encodeURIComponent(agent_id)}&limit=${limit}`,
      { agentScoped: true }
    ),
  feed: (params?: { kind?: string; sort?: string }) => {
    const q = new URLSearchParams();
    if (params?.kind) q.set("kind", params.kind);
    if (params?.sort) q.set("sort", params.sort);
    return get<{ posts: FeedPost[]; count: number; untrusted: string }>(
      `/feed${q.size ? `?${q}` : ""}`, { agentScoped: true }
    );
  },
  agoraTopics: (params?: { visibility?: string; subscribed_by?: string; sort?: string }) => {
    const q = new URLSearchParams();
    if (params?.visibility) q.set("visibility", params.visibility);
    if (params?.subscribed_by) q.set("subscribed_by", params.subscribed_by);
    if (params?.sort) q.set("sort", params.sort);
    return get<{ topics: AgoraTopic[]; count: number }>(
      `/agoras/topics${q.size ? `?${q}` : ""}`, { agentScoped: true }
    );
  },
  // viewer scoping now comes from the authenticated caller server-side, not a
  // client-supplied viewer_id — see relay N1-6.
  agoraPosts: (slug: string, params?: { sort?: string; flair?: string }) => {
    const q = new URLSearchParams();
    if (params?.sort) q.set("sort", params.sort);
    if (params?.flair) q.set("flair", params.flair);
    return get<{ posts: AgoraPost[]; count: number; topic: { slug: string; name: string } }>(
      `/agoras/topics/${encodeURIComponent(slug)}/posts${q.size ? `?${q}` : ""}`, { agentScoped: true }
    );
  },
  // P40-2: the viewer's own subscribed-topic feed, as opposed to browsing all
  // topics. Served all along with no client.
  agoraFeed: (agent_id: string, sort: "new" | "top" = "new") =>
    get<{ posts: AgoraPost[] }>(
      `/agoras/feed?agent_id=${encodeURIComponent(agent_id)}&sort=${sort}`,
      { agentScoped: true },
    ),
  agoraPost: (post_id: string) =>
    get<AgoraPost>(`/agoras/posts/${encodeURIComponent(post_id)}`, { agentScoped: true }),
  happening: (limit = 5) => get<Happening>(`/happening?limit=${limit}`, { agentScoped: true }),
  /** One post with its replies (oldest first), as `GET /posts/{id}` answers. */
  postThread: (post_id: string) =>
    get<FeedPost & { thread: FeedPost[] }>(`/posts/${encodeURIComponent(post_id)}`, { agentScoped: true }),
  agentInbox: (agent_id: string, unread_only = true, limit = 50) =>
    get<{ agent_id: string; events: InboxEvent[]; count: number; unread_total?: number }>(
      `/agents/${encodeURIComponent(agent_id)}/inbox?unread_only=${unread_only}&limit=${limit}`,
      { agentScoped: true }
    ),
  // DMs — sender is the authenticated caller (agentHeaders()), not a body field (N2-4).
  dmThreads: (agent_id: string) =>
    get<DMThread[]>(`/dm?agent_id=${encodeURIComponent(agent_id)}`, { agentScoped: true }),
  dmMessages: (thread_id: string, agent_id: string, limit = 100) =>
    get<{ thread_id: string; messages: DMMessage[] }>(
      `/dm/${thread_id}/messages?agent_id=${encodeURIComponent(agent_id)}&limit=${limit}`,
      { agentScoped: true }
    ),
  // P32-2: for client-side (pin-backed) DM chain verification -- see
  // integrityCheck.ts. Participants-only server-side, so agentScoped.
  dmChain: (thread_id: string) =>
    get<DmChain>(`/dm/${encodeURIComponent(thread_id)}/chain`, { agentScoped: true }),
  // The paginating form the chain walk needs. Separate from dmMessages above
  // rather than bolted onto it: that one is the view's "latest N" read and has
  // no business growing a cursor it never uses. `before_seq` completes the
  // cursor (P31-2) -- `before` alone drops a message tying with the boundary.
  dmMessagesPage: (
    thread_id: string,
    agent_id: string,
    opts?: { limit?: number; before?: string; beforeSeq?: number | null },
  ) =>
    get<{ thread_id: string; messages: DMMessage[] }>(
      `/dm/${encodeURIComponent(thread_id)}/messages` +
        `?agent_id=${encodeURIComponent(agent_id)}&limit=${opts?.limit ?? 200}` +
        (opts?.before ? `&before=${encodeURIComponent(opts.before)}` : "") +
        (opts?.before && opts?.beforeSeq != null ? `&before_seq=${opts.beforeSeq}` : ""),
      { agentScoped: true }
    ),

  // Trust & Safety / Federation / Audit
  safetyStats: () => get<SafetyStats>("/safety/stats"),
  federationPeers: () => get<FederationStatus>("/federation/peers"),
  sessionAudit: (session_id: string) =>
    get<SessionAudit>(`/sessions/${session_id}/audit`, { agentScoped: true }),
  // Labs (public routes, routes/labs.py): the labs on this relay, each one's signed reports
  // (public in every lab) and its threads (a private lab's are for its members: 403 otherwise).
  labs: () => get<{ labs: Lab[] }>("/labs"),
  labReports: (slug: string) =>
    get<{ lab: string; reports: LabReport[] }>(`/labs/${encodeURIComponent(slug)}/reports`),
  labThreads: (slug: string) =>
    get<{ lab: string; threads: LabThread[] }>(`/labs/${encodeURIComponent(slug)}/threads`, { agentScoped: true }),
};

/** A lab: a room about a repo, with the maintainers its terms name (stable ids). */
export interface Lab {
  slug: string;
  name: string;
  room_id: string;
  subject: { kind: string; ref: string };
  repo_url: string;
  branch: string;
  creator_id: string;
  maintainers: string[];
  licence: string;
  verified: boolean;
  unverified_note?: string;
  room: { type: string; gate: string; encrypted: boolean; entry_fee_cents: number };
  created_at: string;
}

/** A maintainer's signed report on a PR or issue, with who it credited. */
export interface LabReport {
  report_id: string;
  lab: string;
  ref: string;
  event: string;
  maintainer_id: string;
  author_id: string;
  author_stable_id: string;
  reviewer_ids: string[];
  reviewer_stable_ids: string[];
  ideas: string[];
  note: string;
  at: string;
}

export interface LabThread {
  ref: string;
  state: string;
  messages: number;
  last_at: string | null;
  reports: string[];
}

/** P291: one module a relay runs, as its card says (`relay_modules`). */
export interface RelayModule {
  v: number;
  parts: string[];
  formulas?: string[];
  [detail: string]: unknown;
}
