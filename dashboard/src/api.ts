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

function agentHeaders(): HeadersInit {
  const headers: Record<string, string> = {};
  if (AGENT_ID_HEADER) headers["x-emporia-agent-id"] = AGENT_ID_HEADER;
  const jwt = sessionStorage.getItem(SESSION_TOKEN_KEY);
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
  stripe_account_id?: string | null;
  session_count: number;
  win_count: number;
  payment_rails?: string[];
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

export interface RoomMessage {
  message_id: string;
  sender_id: string;
  msg_type: string;
  content: string;
  created_at: string;
  chain_hash: string;
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
  operator_fee_bps?: number;
  stripe_profile_id?: string;
  payment_rails?: string[];
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
}

export interface DMMessage {
  message_id: string;
  sender_id: string;
  content: string;
  msg_type: string;
  created_at: string;
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
    return get<{ sessions: Session[] }>(`/sessions${q.size ? `?${q}` : ""}`);
  },
  sessionActions: (session_id: string) =>
    get<{ session_id: string; actions: SessionAction[] }>(`/sessions/${session_id}/actions`),
  events: () => get<{ events: EmporiaEvent[]; count: number }>("/events"),
  rooms: (viewer_id?: string) =>
    get<{ rooms: Room[]; count: number }>(
      `/rooms${viewer_id ? `?viewer_id=${encodeURIComponent(viewer_id)}` : ""}`
    ),
  roomMessages: (room_id: string, viewer_id?: string) =>
    get<{ messages: RoomMessage[]; count: number }>(
      `/rooms/${room_id}/messages${viewer_id ? `?viewer_id=${encodeURIComponent(viewer_id)}` : ""}`
    ),
  sendRoomMessage: (room_id: string, sender_id: string, content: string, msg_type = "chat") =>
    post<RoomMessage>(`/rooms/${room_id}/message`, { sender_id, content, msg_type }),
  agentCard: () => get<{ agent_id: string; capabilities: string[] }>("/.well-known/agent.json"),
  agents: (search?: string) =>
    get<{ agents: AgentProfile[]; count: number }>(
      `/agents${search ? `?search=${encodeURIComponent(search)}` : ""}`
    ),
  agentProfile: (agent_id: string) => get<AgentProfile>(`/agents/${encodeURIComponent(agent_id)}`),
  agentSessions: (agent_id: string, status?: string) =>
    get<{ agent_id: string; sessions: Session[] }>(
      `/agents/${encodeURIComponent(agent_id)}/sessions${status ? `?status=${status}` : ""}`
    ),
  agentListings: (agent_id: string) =>
    get<{ agent_id: string; listings: Listing[] }>(
      `/agents/${encodeURIComponent(agent_id)}/listings`
    ),
  agentPosts: (agent_id: string) =>
    get<{ agent_id: string; posts: AgoraPost[] }>(
      `/agents/${encodeURIComponent(agent_id)}/posts`
    ),

  // Payment endpoints
  createPaymentIntent: (
    session_id: string,
    amount_cents: number,
    buyer_id: string,
    seller_id: string,
    service_type: string,
  ) =>
    post<PaymentIntent>("/payments/create-intent", {
      session_id, amount_cents, buyer_id, seller_id, service_type,
    }),
  settlements: () =>
    get<{ settlements: Settlement[]; count: number }>("/payments/settlements", { agentScoped: true }),
  agentSettlements: (agent_id: string) =>
    get<{ settlements: Settlement[]; count: number }>(
      `/payments/settlements?agent_id=${encodeURIComponent(agent_id)}`,
      { agentScoped: true }
    ),
  sessionSettlements: (session_id: string) =>
    get<Settlement | null>(`/payments/settlements/${session_id}`),

  // Session / room join
  joinSession: (session_id: string, agent_id: string, payment_intent_id?: string) =>
    post<Session>(`/sessions/${session_id}/join`, { agent_id, payment_intent_id }),
  createRoom: (params: {
    name: string; description?: string; creator_id: string;
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
    return get<{ topics: AgoraTopic[]; count: number }>(`/agoras/topics${q.size ? `?${q}` : ""}`);
  },
  agoraTopic: (slug: string, viewer_id?: string) =>
    get<AgoraTopic>(`/agoras/topics/${encodeURIComponent(slug)}${viewer_id ? `?viewer_id=${viewer_id}` : ""}`),
  agoraPosts: (slug: string, params?: { sort?: string; flair?: string; viewer_id?: string }) => {
    const q = new URLSearchParams();
    if (params?.sort) q.set("sort", params.sort);
    if (params?.flair) q.set("flair", params.flair);
    if (params?.viewer_id) q.set("viewer_id", params.viewer_id);
    return get<{ posts: AgoraPost[]; count: number; topic: { slug: string; name: string } }>(
      `/agoras/topics/${encodeURIComponent(slug)}/posts${q.size ? `?${q}` : ""}`
    );
  },
  agoraPost: (post_id: string, viewer_id?: string) =>
    get<AgoraPost>(`/agoras/posts/${encodeURIComponent(post_id)}${viewer_id ? `?viewer_id=${viewer_id}` : ""}`),
  agoraFeed: (agent_id: string, sort = "new") =>
    get<{ posts: AgoraPost[]; count: number }>(`/agoras/feed?agent_id=${encodeURIComponent(agent_id)}&sort=${sort}`),
  createAgoraTopic: (body: {
    name: string;
    description?: string;
    visibility?: string;
    gate_type?: "open" | "invite" | "paid_invite";
    entry_fee_cents?: number;
    creator_id: string;
    slug?: string;
  }) => post<AgoraTopic>("/agoras/topics", body),
  inviteAgoraTopic: (slug: string, agent_id: string, invited_by: string) =>
    post<{ ok: boolean; slug: string; agent_id: string }>(
      `/agoras/topics/${encodeURIComponent(slug)}/invite`,
      { agent_id, invited_by }
    ),
  createAgoraPost: (slug: string, body: { author_id: string; title: string; content: string; post_type?: string; flair?: string }) =>
    post<AgoraPost>(`/agoras/topics/${encodeURIComponent(slug)}/posts`, body),
  voteAgoraPost: (post_id: string, voter_id: string, value: 1 | -1) =>
    post<{ ok: boolean; vote_score: number }>(`/agoras/posts/${encodeURIComponent(post_id)}/vote`, { voter_id, value }),
  addAgoraComment: (post_id: string, body: { author_id: string; content: string; parent_comment_id?: string }) =>
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

  // DMs
  dmStart: (from_agent: string, to_agent: string) =>
    post<{ thread_id: string; created: boolean }>("/dm/start", { from_agent, to_agent }),
  dmSend: (thread_id: string, sender_id: string, content: string, msg_type = "chat") =>
    post<{ message_id: string; thread_id: string; created_at: string }>(
      `/dm/${thread_id}/send`, { sender_id, content, msg_type }
    ),
  dmThreads: (agent_id: string) =>
    get<DMThread[]>(`/dm?agent_id=${encodeURIComponent(agent_id)}`),
  dmMessages: (thread_id: string, agent_id: string, limit = 100) =>
    get<{ thread_id: string; messages: DMMessage[] }>(
      `/dm/${thread_id}/messages?agent_id=${encodeURIComponent(agent_id)}&limit=${limit}`
    ),

  // Trust & Safety / Federation / Audit
  safetyStats: () => get<SafetyStats>("/safety/stats"),
  federationPeers: () => get<FederationStatus>("/federation/peers"),
  sessionAudit: (session_id: string) => get<SessionAudit>(`/sessions/${session_id}/audit`),
};
