const BASE = (() => {
  const env = import.meta.env.VITE_RELAY_URL;
  if (typeof env === "string" && env.length > 0) return env;
  if (typeof window !== "undefined" && window.location?.host) {
    return `${window.location.protocol}//${window.location.host}`;
  }
  return "http://127.0.0.1:8088";
})();

// Sent on requests that scope data to the current agent.
// Local relay: X-Emporia-Agent-Id header trusted on localhost.
// Remote relay: Authorization: Bearer <jwt> from the dashboard/challenge flow.
const AGENT_ID_HEADER = import.meta.env.VITE_AGENT_ID as string | undefined;
const SESSION_TOKEN_KEY = "emporia_dashboard_token";

/** The dashboard's JWT, if any — for callers that need it outside a fetch() header
 * (e.g. WebSocket handshakes, which can't set custom headers from browser JS). */
export function dashboardToken(): string | null {
  return sessionStorage.getItem(SESSION_TOKEN_KEY);
}

function agentHeaders(): HeadersInit {
  const headers: Record<string, string> = {};
  if (AGENT_ID_HEADER) headers["x-emporia-agent-id"] = AGENT_ID_HEADER;
  const jwt = dashboardToken();
  if (jwt) headers["authorization"] = `Bearer ${jwt}`;
  return headers;
}

async function get<T>(path: string, opts?: { agentScoped?: boolean }): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    headers: opts?.agentScoped ? agentHeaders() : {},
  });
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
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

export interface Session {
  session_id: string;
  module_type: string;
  status: string;
  participants: string[];
  current_agent: string;
  step_number: number;
  created_at: string;
  state?: { board_fen?: string; [k: string]: unknown };
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
  participant_count?: number;
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
  mpp_enabled?: boolean;
  tempo_enabled?: boolean;
  operator_fee_bps?: number;
  stripe_profile_id?: string;
  stripe_profile_ready?: boolean;
  stripe_api_version?: string;
  payment_rails?: string[];
  payment_methods?: string[];
  max_total_spend_cents?: number;
  stripe_mpp_admin_notice?: string | null;
}

export interface SafetyStats {
  guardrails_mode: string;
  min_rationale_chars: number;
  bot_fingerprints: string[];
  nemo_guardrails_enabled: boolean;
  nemo_guardrails_model: string | null;
  guardrail_blocks: number;
  nemo_guardrail_blocks: number;
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

export interface Settlement {
  settlement_id: string;
  session_id: string;
  winner_id: string | null;
  total_stake_cents: number;
  platform_fee_cents: number;
  winner_payout_cents: number;
  platform_fee_bps: number;
  transfer_id?: string;
  transfer_status?: string;
  outcome_type?: string;
  status: string;
  created_at: string;
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

export const api = {
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
  sessionActions: (session_id: string) =>
    get<{ session_id: string; actions: SessionAction[] }>(
      `/sessions/${session_id}/actions`, { agentScoped: true }
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
  createPaymentIntent: (
    session_id: string,
    amount_cents: number,
    seller_id: string,
    service_type: string,
  ) =>
    post<PaymentIntent>("/payments/create-intent", {
      session_id, amount_cents, seller_id, service_type,
    }),
  // N9 review R4: N8-3 changed this endpoint's default `limit` from a hardcoded
  // 100 to 50, silently halving FeesView's effective page size (it passed none
  // before N8-3 either, so it never had to think about the cap explicitly).
  // Explicit `limit` here restores that page size rather than raising the
  // relay-wide default, which is 50 on every other list endpoint in this file —
  // making settlements the outlier at 100 would be the wrong fix.
  settlements: (limit = 100) =>
    get<{ settlements: Settlement[]; count: number; has_more: boolean }>(
      `/payments/settlements?limit=${limit}`, { agentScoped: true }
    ),
  agentSettlements: (agent_id: string, limit = 100) =>
    get<{ settlements: Settlement[]; count: number; has_more: boolean }>(
      `/payments/settlements?agent_id=${encodeURIComponent(agent_id)}&limit=${limit}`,
      { agentScoped: true }
    ),
  sessionSettlements: (session_id: string) =>
    // N4-2/R1(a): relay now applies the resource's own visibility rule instead of
    // returning settlements unconditionally. N7 review R3: capped at `limit`
    // (default 50) with `has_more` — was previously unbounded. The response is
    // always the wrapped-array shape below, matching the other two settlement
    // reads (this type was previously `Settlement | null`, which never matched
    // what the endpoint actually returns).
    get<{ settlements: Settlement[]; count: number; has_more: boolean }>(
      `/payments/settlements/${session_id}`, { agentScoped: true }
    ),

  // Session / room join
  joinSession: (session_id: string, agent_id: string, payment_intent_id?: string) =>
    post<Session>(`/sessions/${session_id}/join`, { agent_id, payment_intent_id }),
  // Creator is the authenticated caller (agentHeaders()) — creator_id is no longer a
  // body field (N2-4).
  createRoom: (params: {
    name: string; description?: string;
    room_type?: string; gate_type?: string;
    entry_fee_cents?: number; currency?: string;
    encrypted?: boolean; linked_session_id?: string;
  }) => post<Room>("/rooms", params),
  joinRoom: (room_id: string, agent_id: string, payment_intent_id?: string) =>
    post<{ status: string; room_id: string }>(`/rooms/${room_id}/join`, {
      agent_id, payment_intent_id,
    }),

  // Agoras
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
  agoraTopic: (slug: string) =>
    get<AgoraTopic>(`/agoras/topics/${encodeURIComponent(slug)}`, { agentScoped: true }),
  agoraPosts: (slug: string, params?: { sort?: string; flair?: string }) => {
    const q = new URLSearchParams();
    if (params?.sort) q.set("sort", params.sort);
    if (params?.flair) q.set("flair", params.flair);
    return get<{ posts: AgoraPost[]; count: number; topic: { slug: string; name: string } }>(
      `/agoras/topics/${encodeURIComponent(slug)}/posts${q.size ? `?${q}` : ""}`, { agentScoped: true }
    );
  },
  agoraPost: (post_id: string) =>
    get<AgoraPost>(`/agoras/posts/${encodeURIComponent(post_id)}`, { agentScoped: true }),
  // agent_id must match the authenticated caller server-side now — see relay N1-6.
  agoraFeed: (agent_id: string, sort = "new") =>
    get<{ posts: AgoraPost[]; count: number }>(
      `/agoras/feed?agent_id=${encodeURIComponent(agent_id)}&sort=${sort}`, { agentScoped: true }
    ),
  // Creator is the authenticated caller (agentHeaders()) — creator_id is no longer a
  // body field (N2-4).
  createAgoraTopic: (body: {
    name: string;
    description?: string;
    visibility?: string;
    gate_type?: "open" | "invite" | "paid_invite";
    entry_fee_cents?: number;
    slug?: string;
  }) => post<AgoraTopic>("/agoras/topics", body),
  // Caller (agentHeaders()) must be the topic creator — invited_by is no longer a body
  // field (N2-4).
  inviteAgoraTopic: (slug: string, invitee_id: string) =>
    post<{ ok: boolean; slug: string; agent_id: string }>(
      `/agoras/topics/${encodeURIComponent(slug)}/invite`,
      { invitee_id }
    ),
  // Author is the authenticated caller (agentHeaders()) — author_id is no longer a body
  // field (N2-4).
  createAgoraPost: (slug: string, body: { title: string; content: string; post_type?: string; flair?: string }) =>
    post<AgoraPost>(`/agoras/topics/${encodeURIComponent(slug)}/posts`, body),
  // Voter is the authenticated caller (agentHeaders()) — voter_id is no longer a body
  // field (N2-4).
  voteAgoraPost: (post_id: string, value: 1 | -1) =>
    post<{ ok: boolean; vote_score: number }>(`/agoras/posts/${encodeURIComponent(post_id)}/vote`, { value }),
  addAgoraComment: (post_id: string, body: { content: string; parent_comment_id?: string }) =>
    post<AgoraComment>(`/agoras/posts/${encodeURIComponent(post_id)}/comments`, body),
  subscribeAgoraTopic: (slug: string, agent_id: string) =>
    post<{ ok: boolean }>(`/agoras/topics/${encodeURIComponent(slug)}/subscribe`, { agent_id }),

  // Inbox
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
  dmStart: (to_agent: string) =>
    post<{ thread_id: string; created: boolean }>("/dm/start", { to_agent }),
  // agent_id must match the authenticated caller server-side now — see relay N1-6.
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
