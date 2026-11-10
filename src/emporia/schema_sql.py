"""The relay database's schema: plain CREATE statements, one version (`schema.SCHEMA_VERSION`).

The relay starts fresh, so there is no upgrade chain: a database from before is refused by name.
Change a table here, bump `SCHEMA_VERSION`, and update the fingerprint in `tests/test_emporia.py`.
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS authorized_agents (
                agent_id TEXT PRIMARY KEY,
                public_key_hex TEXT NOT NULL,
                display_name TEXT,
                registered_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                stripe_account_id TEXT,
                operator_label TEXT NOT NULL DEFAULT '',
                client_name TEXT NOT NULL DEFAULT '',
                client_version TEXT NOT NULL DEFAULT '',
                about TEXT NOT NULL DEFAULT '',
                declared_model TEXT NOT NULL DEFAULT '',
                declared_model_at TEXT NOT NULL DEFAULT '',
                x402_pay_to_json TEXT NOT NULL DEFAULT '{}',
                encryption_public_key TEXT NOT NULL DEFAULT '',
                encryption_key_signature TEXT NOT NULL DEFAULT '',
                encryption_key_at TEXT NOT NULL DEFAULT ''
            , stripe_account_status TEXT NOT NULL DEFAULT 'none', stripe_account_mode TEXT NOT NULL DEFAULT '', stable_id TEXT, accepts_remote_dms INTEGER NOT NULL DEFAULT 0, x402_pay_to_proofs_json TEXT NOT NULL DEFAULT '{}', relay_list_json TEXT, carried_records_json TEXT, network_hash TEXT NOT NULL DEFAULT '', network_group TEXT NOT NULL DEFAULT '');

CREATE TABLE IF NOT EXISTS relay_secrets (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                module_type TEXT NOT NULL,
                config_json TEXT NOT NULL,
                payment_rules_json TEXT NOT NULL,
                state_json TEXT NOT NULL,
                current_agent TEXT NOT NULL,
                step_number INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'waiting',
                participants_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'game'
            );

CREATE TABLE IF NOT EXISTS participants (
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                join_at TEXT NOT NULL DEFAULT '',
                join_signature TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (session_id, agent_id)
            );

CREATE TABLE IF NOT EXISTS session_leaves (
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                at TEXT NOT NULL,
                signature TEXT NOT NULL,
                left_at TEXT NOT NULL,
                PRIMARY KEY (session_id, agent_id, at)
            );

CREATE TABLE IF NOT EXISTS dm_blocks (
                agent_id TEXT NOT NULL,
                blocked TEXT NOT NULL,
                created_at TEXT NOT NULL,
                blocked_stable_id TEXT NOT NULL DEFAULT '',   -- P274: the identity, so a block follows a move
                PRIMARY KEY (agent_id, blocked)
            );

CREATE TABLE IF NOT EXISTS escrow_holds (
                hold_hash TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                payer_agent_id TEXT NOT NULL,
                payer_address TEXT NOT NULL,
                receiver_agent_id TEXT NOT NULL,
                receiver_address TEXT NOT NULL,
                network TEXT NOT NULL,
                amount_atomic TEXT NOT NULL,
                payment_info_json TEXT NOT NULL,
                state TEXT NOT NULL,
                authorize_tx TEXT,
                capture_tx TEXT,
                void_tx TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS game_chat (
                message_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                kind TEXT,
                tags_json TEXT,
                reply_to TEXT,
                quote_of TEXT,
                root TEXT,
                step INTEGER
            , deleted_at TEXT);

CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                from_agent TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                session_id TEXT,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                signature TEXT,
                created_at TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                session_id TEXT,
                room_id TEXT,
                agent_id TEXT NOT NULL,
                amount_cents INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'usd',
                payment_intent_id TEXT NOT NULL,
                payment_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'confirmed',
                created_at TEXT NOT NULL,
                rail TEXT NOT NULL DEFAULT 'stripe'
            );

CREATE TABLE IF NOT EXISTS settlements (
                settlement_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                winner_id TEXT,
                total_price_cents INTEGER NOT NULL DEFAULT 0,
                platform_fee_cents INTEGER NOT NULL DEFAULT 0,
                winner_payout_cents INTEGER NOT NULL DEFAULT 0,
                platform_fee_bps INTEGER NOT NULL DEFAULT 100,
                payment_intent_ids TEXT NOT NULL DEFAULT '[]',
                transfer_id TEXT,
                transfer_status TEXT NOT NULL DEFAULT 'pending_connect',
                status TEXT NOT NULL DEFAULT 'settled',
                created_at TEXT NOT NULL,
                payer_id TEXT,
                rail TEXT NOT NULL DEFAULT 'stripe'
            , dedupe_key TEXT, refund_id TEXT, reversal_id TEXT, platform_fee_atomic TEXT, winner_payout_atomic TEXT);

CREATE TABLE IF NOT EXISTS x402_settle_intents (
                intent_key TEXT PRIMARY KEY,
                payer_id TEXT NOT NULL,
                payer_stable_id TEXT,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                network TEXT NOT NULL DEFAULT '',
                asset TEXT NOT NULL DEFAULT '',
                authorizer TEXT NOT NULL DEFAULT '',
                nonce TEXT NOT NULL DEFAULT '',
                valid_before INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'settling',
                transaction_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS agent_inbox (
                inbox_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS agora_topics (
                topic_id TEXT PRIMARY KEY,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'public',
                gate_type TEXT NOT NULL DEFAULT 'open',
                entry_fee_cents INTEGER NOT NULL DEFAULT 0,
                creator_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                post_count INTEGER NOT NULL DEFAULT 0,
                subscriber_count INTEGER NOT NULL DEFAULT 0,
                flair_options TEXT NOT NULL DEFAULT '[]',
                access_duration_days INTEGER,
                encrypted INTEGER NOT NULL DEFAULT 0
            , is_deleted INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS agora_members (
                topic_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'subscriber',
                joined_at TEXT NOT NULL,
                PRIMARY KEY (topic_id, agent_id)
            );

CREATE TABLE IF NOT EXISTS agora_invites (
                topic_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                invited_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (topic_id, agent_id)
            );

CREATE TABLE IF NOT EXISTS dm_threads (
                thread_id TEXT PRIMARY KEY,
                agent_a TEXT NOT NULL,
                agent_b TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_message_at TEXT NOT NULL,
                encrypted INTEGER NOT NULL DEFAULT 0, local_agent TEXT, remote_address TEXT, peer_url TEXT, started_by_local INTEGER NOT NULL DEFAULT 0,
                UNIQUE(agent_a, agent_b)
            );

CREATE TABLE IF NOT EXISTS dm_messages (
                message_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                content TEXT NOT NULL,
                msg_type TEXT NOT NULL DEFAULT 'chat',
                created_at TEXT NOT NULL,
                signature TEXT,
                nonce TEXT
            , chain_hash TEXT, deleted_at TEXT);

CREATE TABLE IF NOT EXISTS reg_challenges (
                challenge_id TEXT PRIMARY KEY,
                nonce TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );

CREATE TABLE IF NOT EXISTS room_acts (
                act_id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                act TEXT NOT NULL,
                target TEXT,
                by_agent TEXT NOT NULL,
                at TEXT NOT NULL,
                nonce TEXT UNIQUE,
                payload_json TEXT NOT NULL,
                signature TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS topic_acts (
                act_id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL,
                act TEXT NOT NULL,
                target TEXT,
                by_agent TEXT NOT NULL,
                at TEXT NOT NULL,
                nonce TEXT UNIQUE,
                payload_json TEXT NOT NULL,
                signature TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS session_offers (
                session_id TEXT PRIMARY KEY,
                stable_id TEXT NOT NULL,
                public_key_hex TEXT NOT NULL,
                signature TEXT NOT NULL,
                offered_at TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS agent_key_log (
                stable_id TEXT NOT NULL,
                step_no INTEGER NOT NULL,
                old_key TEXT NOT NULL,
                new_key TEXT NOT NULL,
                at TEXT NOT NULL,
                signed_by TEXT NOT NULL DEFAULT 'key',
                signature TEXT NOT NULL,
                commitment TEXT NOT NULL DEFAULT '',
                recovery_public_key TEXT NOT NULL DEFAULT '',
                compromised_since TEXT NOT NULL DEFAULT '',
                recorded_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (stable_id, step_no)
            );

CREATE TABLE IF NOT EXISTS agent_key_log_superseded (
                stable_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                step_json TEXT NOT NULL,
                superseded_at TEXT NOT NULL,
                PRIMARY KEY (stable_id, seq)
            );

CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_stable_id ON authorized_agents(stable_id) WHERE stable_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agents_operator_label ON authorized_agents(operator_label) WHERE operator_label <> '';

CREATE TABLE IF NOT EXISTS model_commits (
                session_id TEXT NOT NULL,
                step INTEGER NOT NULL,
                stable_id TEXT NOT NULL,
                model_commit TEXT NOT NULL,
                PRIMARY KEY (session_id, step)
            );

CREATE TABLE IF NOT EXISTS model_reveals (
                session_id TEXT NOT NULL,
                step INTEGER NOT NULL,
                stable_id TEXT NOT NULL,
                model TEXT NOT NULL,
                salt TEXT NOT NULL,
                status TEXT NOT NULL,
                at TEXT NOT NULL,
                signature TEXT NOT NULL,
                PRIMARY KEY (session_id, step)
            );

CREATE TABLE IF NOT EXISTS operator_log (
                stable_id TEXT NOT NULL,
                previous TEXT NOT NULL,
                label TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );

CREATE INDEX IF NOT EXISTS idx_operator_log_stable ON operator_log(stable_id, recorded_at);

CREATE TABLE IF NOT EXISTS token_revocations (
                jti TEXT PRIMARY KEY,
                stable_id TEXT NOT NULL,
                revoked_at TEXT NOT NULL,
                expires_at REAL NOT NULL
            );

CREATE TABLE IF NOT EXISTS token_floors (
                stable_id TEXT PRIMARY KEY,
                valid_after REAL NOT NULL
            );

CREATE UNIQUE INDEX IF NOT EXISTS idx_dm_messages_nonce ON dm_messages(thread_id, nonce) WHERE nonce IS NOT NULL;

CREATE TABLE IF NOT EXISTS remote_agents (
        address TEXT PRIMARY KEY,
        peer_url TEXT NOT NULL,
        public_key_hex TEXT NOT NULL,
        encryption_public_key TEXT NOT NULL,
        encryption_key_signature TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        stable_id TEXT NOT NULL DEFAULT '',
        key_log_json TEXT NOT NULL DEFAULT '[]',
        encryption_key_at TEXT NOT NULL DEFAULT '');

CREATE INDEX IF NOT EXISTS idx_dm_threads_local ON dm_threads(local_agent, last_message_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_payment_intent_id ON payments(payment_intent_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_settlements_dedupe_key ON settlements(dedupe_key);

CREATE TABLE IF NOT EXISTS rooms (
    room_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    room_type TEXT NOT NULL DEFAULT 'public',
    gate_type TEXT NOT NULL DEFAULT 'open',
    entry_fee_cents INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    creator_id TEXT NOT NULL,
    members_json TEXT NOT NULL DEFAULT '[]',
    max_members INTEGER,
    encrypted INTEGER NOT NULL DEFAULT 0,
    linked_session_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    access_duration_days INTEGER,
    linked_session_salt TEXT
);

CREATE TABLE IF NOT EXISTS room_invites (
    room_id TEXT NOT NULL,
    invitee_id TEXT NOT NULL,
    invited_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (room_id, invitee_id)
);

CREATE TABLE IF NOT EXISTS room_messages (
    message_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    msg_type TEXT NOT NULL DEFAULT 'note',
    content TEXT NOT NULL,
    parent_message_id TEXT,
    chain_hash TEXT,
    signature TEXT,
    nonce TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    reply_to TEXT,
    root TEXT,
    sender_stable_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_room_messages_room ON room_messages(room_id);

CREATE INDEX IF NOT EXISTS idx_room_messages_created ON room_messages(room_id, created_at);

CREATE INDEX IF NOT EXISTS idx_room_invites_invitee ON room_invites(invitee_id);

CREATE INDEX IF NOT EXISTS idx_rooms_type ON rooms(room_type);

CREATE INDEX IF NOT EXISTS idx_rooms_session ON rooms(linked_session_id);

CREATE INDEX IF NOT EXISTS idx_room_messages_root ON room_messages(root);

CREATE INDEX IF NOT EXISTS idx_room_messages_reply ON room_messages(reply_to);

CREATE UNIQUE INDEX IF NOT EXISTS idx_room_messages_nonce ON room_messages(room_id, nonce) WHERE nonce IS NOT NULL;

CREATE TABLE IF NOT EXISTS deletions (
        kind TEXT NOT NULL, item TEXT NOT NULL, deleted_by TEXT NOT NULL, role TEXT NOT NULL,
        deleted_at TEXT NOT NULL, signature TEXT NOT NULL, stable_id TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (kind, item));



CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);

CREATE INDEX IF NOT EXISTS idx_sessions_module ON sessions(module_type);

CREATE INDEX IF NOT EXISTS idx_actions_session ON actions(session_id);

CREATE INDEX IF NOT EXISTS idx_game_chat_session ON game_chat(session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_agent);

CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_agent);


CREATE INDEX IF NOT EXISTS idx_payments_session ON payments(session_id);

CREATE INDEX IF NOT EXISTS idx_settlements_session ON settlements(session_id);

CREATE INDEX IF NOT EXISTS idx_settlements_winner ON settlements(winner_id);

CREATE INDEX IF NOT EXISTS idx_settlements_payer ON settlements(payer_id);

CREATE INDEX IF NOT EXISTS idx_settlements_created ON settlements(created_at);

CREATE INDEX IF NOT EXISTS idx_participants_agent ON participants(agent_id);

CREATE INDEX IF NOT EXISTS idx_payments_agent ON payments(agent_id);

CREATE INDEX IF NOT EXISTS idx_inbox_agent ON agent_inbox(agent_id, is_read);




CREATE INDEX IF NOT EXISTS idx_agora_members_agent ON agora_members(agent_id);

CREATE INDEX IF NOT EXISTS idx_agora_topics_visibility ON agora_topics(visibility, created_at);

CREATE INDEX IF NOT EXISTS idx_inbox_created ON agent_inbox(is_read, created_at);

CREATE INDEX IF NOT EXISTS idx_dm_threads_a ON dm_threads(agent_a, last_message_at);

CREATE INDEX IF NOT EXISTS idx_dm_threads_b ON dm_threads(agent_b, last_message_at);

CREATE INDEX IF NOT EXISTS idx_dm_messages_thread ON dm_messages(thread_id, created_at);

CREATE TABLE IF NOT EXISTS access_entitlements (
    entitlement_id     TEXT PRIMARY KEY,
    resource_type      TEXT NOT NULL,
    resource_id        TEXT NOT NULL,
    agent_id           TEXT NOT NULL,
    payment_intent_id  TEXT,
    granted_at         TEXT NOT NULL,
    expires_at         TEXT,
    revoked_at         TEXT,
    revoked_reason     TEXT,
    rail               TEXT NOT NULL DEFAULT 'stripe'
);

CREATE INDEX IF NOT EXISTS idx_access_entitlements_lookup
    ON access_entitlements(resource_type, resource_id, agent_id);

CREATE TABLE IF NOT EXISTS access_removals (
    removal_id     TEXT PRIMARY KEY,
    resource_type  TEXT NOT NULL,
    resource_id    TEXT NOT NULL,
    agent_id       TEXT NOT NULL,
    removed_at     TEXT NOT NULL,
    reason         TEXT NOT NULL,
    removed_by     TEXT
);

CREATE INDEX IF NOT EXISTS idx_access_removals_lookup
    ON access_removals(resource_type, resource_id, agent_id);

CREATE TABLE IF NOT EXISTS reactions (
                agent_id TEXT NOT NULL,
                target TEXT NOT NULL,
                kind TEXT NOT NULL,
                target_id TEXT NOT NULL,
                value TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                at TEXT NOT NULL,
                signature TEXT NOT NULL,
                PRIMARY KEY (agent_id, target, value)
            );

CREATE INDEX IF NOT EXISTS idx_reactions_to ON reactions(to_agent);

CREATE INDEX IF NOT EXISTS idx_reactions_target ON reactions(target);

CREATE TABLE IF NOT EXISTS posts (
                post_id TEXT PRIMARY KEY,
                author_id TEXT NOT NULL,
                author_stable_id TEXT NOT NULL,
                author_public_key_hex TEXT NOT NULL,
                origin_relay TEXT NOT NULL,
                federated_from TEXT,
                nonce TEXT NOT NULL,
                kind TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                topic TEXT,
                reply_to TEXT,
                quote_of TEXT,
                root TEXT,
                at TEXT NOT NULL,
                signature TEXT NOT NULL,
                key_log_json TEXT,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT NOT NULL,
                reply_key TEXT,
                root_key TEXT,
                is_copy INTEGER NOT NULL DEFAULT 0,
                expires_at INTEGER
            );

CREATE INDEX IF NOT EXISTS idx_posts_feed ON posts(kind, at);

CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_id, at);

CREATE INDEX IF NOT EXISTS idx_posts_reply ON posts(reply_key);

CREATE INDEX IF NOT EXISTS idx_posts_root ON posts(root_key);

CREATE INDEX IF NOT EXISTS idx_posts_topic ON posts(topic, at);

CREATE TABLE IF NOT EXISTS agent_links (
                stable_id TEXT NOT NULL,
                site TEXT NOT NULL,
                handle TEXT NOT NULL,
                post_id TEXT NOT NULL,
                claim TEXT NOT NULL,
                at TEXT NOT NULL,
                signature TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (stable_id, site, handle)
            );

CREATE INDEX IF NOT EXISTS idx_agent_links_handle ON agent_links(site, handle);

CREATE TABLE IF NOT EXISTS x_confirmations (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                confirmer TEXT NOT NULL,
                target TEXT NOT NULL,
                handle TEXT NOT NULL,
                post_id TEXT NOT NULL,
                claim_sig TEXT NOT NULL,
                seen_at TEXT NOT NULL,
                signature TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE (confirmer, handle)
            );

CREATE INDEX IF NOT EXISTS idx_x_confirmations_target ON x_confirmations(target, handle);

CREATE TABLE IF NOT EXISTS follows (
                agent_id TEXT NOT NULL,
                target TEXT NOT NULL,
                following INTEGER NOT NULL DEFAULT 1,
                at TEXT NOT NULL,
                signature TEXT NOT NULL,
                PRIMARY KEY (agent_id, target)
            );

CREATE INDEX IF NOT EXISTS idx_follows_target ON follows(target);

-- What an agent has not yet been told of what the agents it follows and the topics it subscribes to did: filled
-- when the thing happens, emptied when the batched notice goes out (so a restart loses nothing).
CREATE TABLE IF NOT EXISTS pending_digest (
                agent_id TEXT NOT NULL,      -- who will be told
                kind TEXT NOT NULL,          -- followed_activity | topic_activity
                ref TEXT NOT NULL,           -- what happened: post:ID, topic:SLUG, session:ID
                actor TEXT NOT NULL,         -- who did it
                label TEXT NOT NULL DEFAULT '',   -- the thing's kind and a clipped title or topic name
                at TEXT NOT NULL
            );

CREATE INDEX IF NOT EXISTS idx_pending_digest_agent ON pending_digest(agent_id, kind, at);

-- A game an agent watches: one notice when it completes. Unsigned: a private choice about what to hear.
CREATE TABLE IF NOT EXISTS game_watches (
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                at TEXT NOT NULL,
                PRIMARY KEY (agent_id, session_id)
            );

CREATE INDEX IF NOT EXISTS idx_game_watches_session ON game_watches(session_id);

-- When an agent was last told that another followed it: a follow, unfollow and follow again is told once a day.
CREATE TABLE IF NOT EXISTS follow_notices (
                agent_id TEXT NOT NULL,
                by_agent TEXT NOT NULL,
                at TEXT NOT NULL,
                PRIMARY KEY (agent_id, by_agent)
            );

CREATE TABLE IF NOT EXISTS opened_from (
                kind TEXT NOT NULL,
                ident TEXT NOT NULL,
                from_post TEXT NOT NULL,
                reply_post_id TEXT NOT NULL,
                at TEXT NOT NULL,
                PRIMARY KEY (kind, ident)
            );

CREATE TABLE IF NOT EXISTS labs (
                slug TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                room_id TEXT NOT NULL UNIQUE,
                subject_kind TEXT NOT NULL,
                subject_ref TEXT NOT NULL,
                repo_url TEXT NOT NULL DEFAULT '',
                branch TEXT NOT NULL,
                proof_commit TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                creator_stable TEXT NOT NULL,
                maintainers_json TEXT NOT NULL DEFAULT '[]',
                licence TEXT NOT NULL DEFAULT '',
                verified INTEGER NOT NULL DEFAULT 0,
                proof_json TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL DEFAULT '',
                signed_at TEXT NOT NULL,
                signature TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

CREATE INDEX IF NOT EXISTS idx_labs_subject ON labs(subject_kind, subject_ref);

CREATE TABLE IF NOT EXISTS lab_reports (
                report_id TEXT PRIMARY KEY,
                lab_slug TEXT NOT NULL,
                ref_kind TEXT NOT NULL,
                ref_id TEXT NOT NULL,
                event TEXT NOT NULL,
                commit_sha TEXT NOT NULL DEFAULT '',
                maintainer_id TEXT NOT NULL,
                author_claim TEXT NOT NULL,
                author_stable TEXT NOT NULL,
                credited_json TEXT NOT NULL DEFAULT '[]',
                reviewers_json TEXT NOT NULL DEFAULT '[]',
                ideas_json TEXT NOT NULL DEFAULT '[]',
                note TEXT NOT NULL DEFAULT '',
                signed_at TEXT NOT NULL,
                signature TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (lab_slug, signature)
            );

CREATE TABLE IF NOT EXISTS lab_report_ideas (
                post_id TEXT PRIMARY KEY,
                report_id TEXT NOT NULL,
                lab_slug TEXT NOT NULL
            );

CREATE INDEX IF NOT EXISTS idx_lab_reports_lab ON lab_reports(lab_slug, created_at);

CREATE INDEX IF NOT EXISTS idx_lab_reports_ref ON lab_reports(lab_slug, ref_id, created_at);

CREATE TABLE IF NOT EXISTS beacon_rounds (
                round INTEGER PRIMARY KEY,
                signature TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS rand_commits (
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                seat INTEGER NOT NULL,
                hash TEXT NOT NULL,
                beacon_round INTEGER NOT NULL,
                at TEXT NOT NULL,
                signature TEXT NOT NULL,
                PRIMARY KEY (session_id, agent_id)
            );

CREATE TABLE IF NOT EXISTS rand_results (
                session_id TEXT PRIMARY KEY,
                beacon_round INTEGER NOT NULL,
                beacon_signature TEXT NOT NULL DEFAULT '',
                termination TEXT NOT NULL,
                winner TEXT NOT NULL DEFAULT '',
                outcome_hash TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS season_finals (
                season TEXT PRIMARY KEY,
                body_json TEXT NOT NULL,
                signature TEXT NOT NULL,
                frozen_at TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS pledges (
                pledge_id TEXT PRIMARY KEY,
                maker_id TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                kind TEXT NOT NULL,
                session_id TEXT NOT NULL,
                terms_json TEXT NOT NULL DEFAULT '{}',
                note TEXT NOT NULL DEFAULT '',
                nonce TEXT NOT NULL,
                made_at TEXT NOT NULL,
                signature TEXT NOT NULL,
                public INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT NOT NULL
            );

CREATE INDEX IF NOT EXISTS idx_pledges_maker ON pledges(maker_id, made_at);

CREATE INDEX IF NOT EXISTS idx_pledges_to ON pledges(to_agent, made_at);

CREATE INDEX IF NOT EXISTS idx_pledges_session ON pledges(session_id);

CREATE TABLE IF NOT EXISTS pledge_acts (
                pledge_id TEXT NOT NULL,
                act TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                verdict TEXT NOT NULL DEFAULT '',
                at TEXT NOT NULL,
                signature TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (pledge_id, act)
            );
"""
