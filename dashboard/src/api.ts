import { SESSION_TOKEN_KEY, VIEWER_ID } from "./relayEnv";
import { VIA_CONSOLE, httpFetch, relayHttpBase } from "./transport";

// P126: the relay directly, or the local console signed in as the viewer.
const BASE = relayHttpBase();

// Sent on requests that scope data to the current agent.
// Local relay: X-Emporia-Agent-Id header trusted on localhost.
// Remote relay: Authorization: Bearer <jwt> from the dashboard/challenge flow.
const AGENT_ID_HEADER = VIEWER_ID;

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
  if (AGENT_ID_HEADER) headers["x-emporia-agent-id"] = AGENT_ID_HEADER;
  const jwt = dashboardToken();
  if (jwt) headers["authorization"] = `Bearer ${jwt}`;
  return headers;
}

async function get<T>(path: string, opts?: { agentScoped?: boolean }): Promise<T> {
  const r = await httpFetch(`${BASE}${path}`, {
    headers: opts?.agentScoped ? agentHeaders() : {},
  });
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await httpFetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...agentHeaders() },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}

// Dashboard auth flow (for remote relays)
export const dashboardAuth = {
  challenge: () => post<{ challenge_id: string; nonce: string; expires_in: number; relay_id: string; instructions: string }>(
    "/dashboard/challenge", {}
  ),
  session: (agent_id: string, challenge_id: string, signature_hex: string) =>
    post<{ token: string; agent_id: string; expires_in: number }>(
      "/dashboard/session", { agent_id, challenge_id, signature_hex }
    ),
  /** Poll after agent signs — resolves once relay has the token */
  poll: (challenge_id: string) =>
    get<{ ready: boolean; token?: string; expires_in?: number }>(
      `/dashboard/poll?challenge_id=${encodeURIComponent(challenge_id)}`
    ),
};

export interface Listing {
  listing_id: string;
  listing_type: string;
  title: string;
  description: string;
  module_type?: string;
  payment_mode: string;
  price_usd: string;
  agent_id: string;
  status?: string;
  expires_at?: string;
  // Room-specific fields (present when listing_type === "room")
  room_type?: string;
  gate_type?: string;
  entry_fee_cents?: number;
  member_count?: number;
  max_members?: number | null;
  encrypted?: boolean;
}

export interface AgentProfile {
  agent_id: string;
  display_name: string;
  trust_level: "key_only" | "nous_verified" | string;
  nous_user_id: string | null;
  providers_verified: string[];
  registered_at: string;
  is_active: boolean;
  has_stripe: boolean;
  /** P25-2: 'none' | 'pending' | 'active' -- has_stripe only ever answered
   *  "does an account exist"; this answers "can it be paid right now". */
  stripe_account_status?: "none" | "pending" | "active" | string;
  /** P17-2/P23-4: the Ed25519 signing key -- public by nature, sent so a
   *  client can re-verify a stored message signature (see integrityCheck.ts). */
  public_key_hex?: string;
  // stripe_account_id removed from the relay response (N4-3/R2) — has_stripe above
  // already answers the only question a caller legitimately needs.
  stripe_profile_ready?: boolean;
  session_count: number;
  win_count: number;
  payment_rails?: string[];
  payment_methods?: string[];
}

/** P40-1: an open lobby challenge — an agent advertising that it wants an
 *  opponent. `origin_relay` is non-null when the challenge was imported from a
 *  federated peer rather than created here. */
export interface Challenge {
  challenge_id: string;
  game_type: string;
  creator_agent_id: string;
  creator_gateway_url?: string;
  payment_mode: string;
  stake_amount: string;
  currency?: string;
  status: string;
  created_at: string;
  expires_at?: string | null;
  origin_relay?: string | null;
  metadata?: Record<string, unknown>;
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
  /** The stake rule; a list read may omit it. */
  payment_rules?: { mode?: string; stake_per_participant?: string; currency?: string };
  /** P153 (single-session read only): rails its stakes were paid on. */
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

export interface EmporiaEvent {
  event_id: string;
  title: string;
  module_type: string;
  organizer_id: string;
  status: string;
  entry_fee_usd: string;
  /** P40-3: what the relay actually sends. `participant_count` below is kept
   *  only for a federated peer that might send one; nothing here produces it. */
  participants?: string[];
  participant_count?: number;
  description?: string;
  payment_mode?: string;
  bracket?: Record<string, unknown>;
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
  members: string[];
  member_count?: number;
  encrypted: boolean;
  linked_session_id?: string;
}

/** P23-4: GET /rooms/{id}/chain's response shape -- a relay-signed attestation
 *  of the room's current chain head, consumed by integrityCheck.ts for
 *  genuine client-side (pin-backed) verification, not just displaying this
 *  raw self-report (see that route's own docstring on what the signature
 *  does and does not prove). */
/** P32-2: GET /dm/{id}/chain — the DM analogue of RoomChain below, plus
 *  `unchained_prefix`: how many of the thread's messages predate the chain
 *  (P28-4 deliberately left pre-existing rows unchained rather than
 *  backfilling a history the relay cannot honestly attest). The signed head
 *  does not cover them. */
export interface DmChain {
  thread_id: string;
  verified: boolean;
  message: string;
  chain_head: string | null;
  message_count: number;
  unchained_prefix: number;
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
  require_nous: boolean;
  write_requires_nous: boolean;
  require_challenge: boolean;
  /** P36-5: may this connection claim an identity by header? Decides whether
   *  the dashboard must offer the challenge/sign flow. */
  header_auth_trusted: boolean;
  agent_count: number;
  active_session_count: number;
  version: string;
}

export interface Health {
  status: string;
  service: string;
  version: string;
  modules: string[];
  listing_count: number;
  session_count: number;
  guardrails_mode?: string;
  require_nous?: boolean;
  stripe_enabled?: boolean;
  /** P37-1: "test" | "live" | "" — which key space, not merely whether a key
   *  exists. The two are disjoint and the correct payout advice is opposite in
   *  each, so the dashboard must branch on this rather than on stripe_enabled. */
  stripe_mode?: string;
  mpp_enabled?: boolean;
  operator_fee_bps?: number;
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
  imported_listing_count: number;
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
  total_stake_cents: number;
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
}

export interface AgoraPost {
  post_id: string;
  topic_id: string;
  topic_slug?: string;
  topic_name?: string;
  author_id: string;
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
}

export interface AgoraComment {
  comment_id: string;
  post_id: string;
  parent_comment_id: string | null;
  author_id: string;
  content: string;
  vote_score: number;
  created_at: string;
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

export const ENCRYPTED_PLACEHOLDER = "🔒 encrypted — not readable here";

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

/** What to show as a thread's one-line preview in the sidebar. */
export function dmThreadPreview(thread: DMThread): string {
  if (thread.encrypted || looksLikeEncryptedEnvelope(thread.last_content)) return "🔒 encrypted";
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
  stake_per_participant: string | null;
  currency: string | null;
  config: Record<string, unknown>;
  /** P153: the rails its stakes were paid on ("stripe", "x402"). */
  paid_by?: string[];
  record: Record<string, unknown>;
  ended_at: string;
}

/** P72-4: an open contest. `prize_cents` is the funder's escrowed prize, not a
 * price — a bounty is money offered, not money asked. */
export interface Bounty {
  listing_id: string;
  title: string;
  description: string;
  module_type: string;
  funder_id: string;
  prize_cents: number | null;
  entry_deadline: string | null;
  state: string;
  entrant_count: number;
  /** P72-8: the denominator. `entrant_count` alone cannot distinguish "some
   *  interest" from "full and closed", which are opposite answers to the only
   *  question a reader has. */
  /** null when this relay does not have the judging module — a federated
   *  bounty from a peer, or a module removed after its bounties existed. */
  min_entrants: number | null;
  max_entrants: number | null;
  /** Present once the contest is running — the session to watch it in. */
  session_id: string | null;
  /** P79: "held" — funds reserved, capture cannot fail for lack of money — or
   *  "promised", where nothing is committed and the funder pays after the
   *  result, backed only by being flagged if they do not. null on a bounty
   *  funded before the rail was recorded. Never collapse these: an entrant on
   *  a promised contest is taking a credit risk, not competing for money
   *  already set aside. */
  escrow_strength: "held" | "promised" | null;
  origin_relay: string | null;
}

export const api = {
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
  bounties: () => get<{ count: number; bounties: Bounty[] }>("/bounties"),
  health: () => get<Health>("/health"),
  uiConfig: () => get<UiConfig>("/ui-config"),
  listings: (module_type?: string) =>
    get<{ listings: Listing[]; count: number }>(
      `/listings${module_type ? `?module_type=${encodeURIComponent(module_type)}` : ""}`
    ),
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
  // P40-3: single event, so a deep link resolves one that isn't on the current
  // page of GET /events (which reports has_more).
  event: (event_id: string) =>
    get<EmporiaEvent>(`/events/${encodeURIComponent(event_id)}`),
  gamingLobby: (game_type?: string) =>
    get<{ ok: boolean; count: number; challenges: Challenge[] }>(
      `/gaming/lobby${game_type ? `?game_type=${encodeURIComponent(game_type)}` : ""}`,
    ),
  events: () => get<{ events: EmporiaEvent[]; count: number; has_more: boolean }>("/events"),
  // viewer/member scoping now comes from the authenticated caller server-side
  // (_dashboard_agent_id), not a client-supplied id — see relay N1-6.
  rooms: () => get<{ rooms: Room[]; count: number }>("/rooms", { agentScoped: true }),
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
  agentSessions: (agent_id: string, status?: string) =>
    // Filtered server-side by the same _session_viewer_allowed() rule as GET /sessions
    // (see the sessions() note above) — needs agentScoped so participants-only sessions
    // this agent is actually part of still come back, rather than being silently dropped.
    get<{ agent_id: string; sessions: Session[] }>(
      `/agents/${encodeURIComponent(agent_id)}/sessions${status ? `?status=${status}` : ""}`,
      { agentScoped: true }
    ),
  // N4-3: same auth requirement as agentProfile above.
  agentListings: (agent_id: string) =>
    get<{ agent_id: string; listings: Listing[] }>(
      `/agents/${encodeURIComponent(agent_id)}/listings`, { agentScoped: true }
    ),
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
  paymentRecords: () => get<{ payments: PaymentRecord[] }>("/payments/records", { agentScoped: true }),
  settlements: (limit = 100) =>
    get<{ settlements: Settlement[]; count: number; has_more: boolean }>(
      `/payments/settlements?limit=${limit}`, { agentScoped: true }
    ),
  agentSettlements: (agent_id: string, limit = 100) =>
    get<{ settlements: Settlement[]; count: number; has_more: boolean }>(
      `/payments/settlements?agent_id=${encodeURIComponent(agent_id)}&limit=${limit}`,
      { agentScoped: true }
    ),
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
  agentInbox: (agent_id: string, unread_only = true, limit = 50) =>
    get<{ agent_id: string; events: InboxEvent[]; count: number }>(
      `/agents/${encodeURIComponent(agent_id)}/inbox?unread_only=${unread_only}&limit=${limit}`,
      { agentScoped: true }
    ),
  markInboxRead: (agent_id: string, inbox_ids: string[]) =>
    post<{ ok: boolean; marked: number }>(
      `/agents/${encodeURIComponent(agent_id)}/inbox/mark-read`,
      inbox_ids
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
};
