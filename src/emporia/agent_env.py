"""Every setting and key name the agent's MCP server may read, so Hermes can hand it each
one (catalog rule 11).

Hermes starts the MCP server with a filtered environment plus its config `env`, and fills
each `${NAME}` there from the profile's secret scope (its .env and secret sources); an
unset `${NAME}` is dropped. So `hermes emporia setup` writes a `${NAME}` for every name the
runtime may read, and the server runs with EMPORIA_NO_DOTENV=1: it reads no .env at all.

The list is the union of:
* LITERAL_NAMES, every EMPORIA_* the code names and every other name it reads, kept
  complete by tests/test_agent_env.py (it scans src/emporia and fails on a name missing);
* the names built from a network id (EMPORIA_X402_ASSET_/EXTRA_/RPC_ + the network, for
  every network in networks.NETWORKS);
* the key names (key_source.ENV_KEY_NAMES, .SOURCE_VARS), the vault tokens and their
  companions a linked key's CLI needs;
* any `env:NAME` a key source points at, and any EMPORIA_* the profile holds now.

A name the profile holds that is on none of these lists is what `emporia doctor` names, with
`emporia agent refresh NAME` to add it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# The settings that are literal values in the MCP entry, not references.
LITERAL_IN_ENTRY = ("EMPORIA_RELAY_URL", "EMPORIA_AGENT_ID", "EMPORIA_DISPLAY_NAME", "EMPORIA_MCP_TRANSPORT",
                    "EMPORIA_KEYS_DIR", "EMPORIA_DB_PATH", "EMPORIA_DOTENV", "EMPORIA_HERMES_BIN",
                    "EMPORIA_NO_DOTENV")
# The gateway adapter's own settings (plugin.yaml's optional_env), which no runtime process
# reads: setup writes them to the profile after the MCP entry, and they are not the server's to
# be handed (found by the end-to-end run: doctor called them "not handed"). Kept off the
# server's list; tests/test_agent_env.py checks the runtime reads none of them.
GATEWAY_ONLY = ("EMPORIA_PYTHON", "EMPORIA_PROFILE_ID", "EMPORIA_ALLOWED_USERS", "EMPORIA_ALLOW_ALL_USERS",
                "EMPORIA_THREAD_LIMIT")
# What a linked key's vault CLI reads besides EMPORIA_* (as the plugin's adapter._VAULT_PREFIXES).
VAULT_PREFIXES = ("OP_", "BW_", "VAULT_", "DOPPLER_", "INFISICAL_", "AWS_", "AZURE_")
VAULT_NAMES = ("OP_SERVICE_ACCOUNT_TOKEN", "OP_CONNECT_HOST", "OP_CONNECT_TOKEN", "BW_SESSION",
               "BW_CLIENTID", "BW_CLIENTSECRET", "VAULT_ADDR", "VAULT_TOKEN", "VAULT_NAMESPACE",
               "DOPPLER_TOKEN", "INFISICAL_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
               "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE",
               "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID")
NETWORK_PREFIXES = ("EMPORIA_X402_ASSET_", "EMPORIA_X402_EXTRA_", "EMPORIA_X402_RPC_")
_ENV_REF = re.compile(r"^\s*env:\s*([A-Z][A-Z0-9_]*)\s*$")

LITERAL_NAMES = (
    "BOT_FINGERPRINTS", "EMPORIA_A2A_URL", "EMPORIA_AGENT_ID", "EMPORIA_ALLOW_HOT_AGENT_WALLET",
    "EMPORIA_ALLOW_HOT_OPERATOR_KEY", "EMPORIA_ALLOW_LIVE_RELAY_IN_PROFILE", "EMPORIA_AUDIT_DIR",
    "EMPORIA_BACKUP_RELAYS", "EMPORIA_BOUNTY_RECONCILE_INTERVAL_SECONDS",
    "EMPORIA_CARD_GAME_STAKES", "EMPORIA_CHAIN_VERIFY_LIMIT", "EMPORIA_CONSOLE_PORT",
    "EMPORIA_CORS_ORIGINS", "EMPORIA_CUSTODIAL_CARDS", "EMPORIA_DAILY_TURNS",
    "EMPORIA_DASHBOARD_BASE_URL", "EMPORIA_DB_PATH", "EMPORIA_DISPLAY_NAME",
    "EMPORIA_DM_RETENTION_DAYS", "EMPORIA_DOTENV", "EMPORIA_EMPORIA_MAX_WEBSOCKETS_PER_ADDRESS",
    "EMPORIA_ENCRYPTION_KEY_SOURCE", "EMPORIA_ENCRYPTION_PRIVATE_KEY",
    "EMPORIA_ENCRYPTION_RETIRED_KEYS", "EMPORIA_ENGINE_GAME_STAKES", "EMPORIA_ESCROW_COLLECTOR",
    "EMPORIA_ESCROW_CONTRACT", "EMPORIA_ESCROW_FEE_RECEIVER", "EMPORIA_ESCROW_NETWORK",
    "EMPORIA_ESCROW_RPC_URL", "EMPORIA_EXPEDITIONS_TOPIC", "EMPORIA_FEDERATED_DMS",
    "EMPORIA_FEDERATION_SYNC_INTERVAL_SECONDS", "EMPORIA_GAMIFY", "EMPORIA_GUARDRAILS_MODE",
    "EMPORIA_HANDED_NAMES", "EMPORIA_HERMES_BIN", "EMPORIA_HOUSE_AGENTS", "EMPORIA_HTTP_TIMEOUT",
    "EMPORIA_IDEA_POINTS", "EMPORIA_IDENTITY_KEY_SOURCE", "EMPORIA_IDENTITY_PRIVATE_KEY",
    "EMPORIA_INSIGHT_MIN_CALLS", "EMPORIA_KEYS_DIR", "EMPORIA_KEY_STORAGE",
    "EMPORIA_LABS", "EMPORIA_LAB_ALLOW_UNISOLATED", "EMPORIA_LAB_HOSTS", "EMPORIA_LAB_PYTHON",
    "EMPORIA_LAB_SANDBOX", "EMPORIA_LAB_WORKDIR", "EMPORIA_LOG_DIR",
    "EMPORIA_LOG_LEVEL", "EMPORIA_LOG_STATE_ENTRIES", "EMPORIA_LOG_STATE_INTERVAL_SECONDS",
    "EMPORIA_MAX_FEDERATION_RESPONSE_BYTES", "EMPORIA_MAX_OPEN_GAMES", "EMPORIA_MAX_REQUEST_BYTES",
    "EMPORIA_MAX_TOTAL_SPEND_CENTS", "EMPORIA_MAX_WEBSOCKETS_PER_ADDRESS", "EMPORIA_MCP_PORT",
    "EMPORIA_MCP_TRANSPORT", "EMPORIA_MODULES", "EMPORIA_MPP_TEMPO_ENABLED", "EMPORIA_NOUS_JWT",
    "EMPORIA_NOUS_VERIFY", "EMPORIA_NO_ARCHIVE", "EMPORIA_NO_DOTENV", "EMPORIA_OPERATOR_ADDRESS",
    "EMPORIA_OPERATOR_PRIVATE_KEY", "EMPORIA_OPERATOR_SIGNER", "EMPORIA_OPERATOR_VERDICT_KEY",
    "EMPORIA_PAYMENT_APPROVAL_CENTS",
    "EMPORIA_PAYOUT_ADDRESSES", "EMPORIA_PAYOUT_RECONCILE_INTERVAL_SECONDS", "EMPORIA_PLAIN_MENUS",
    "EMPORIA_PLAYBOOK", "EMPORIA_PLAYBOOK_ANSWERS", "EMPORIA_PLAYBOOK_BUYS",
    "EMPORIA_PLAYBOOK_GAMES", "EMPORIA_PLAYBOOK_LABS", "EMPORIA_PLAYBOOK_MAX_STAKE_CENTS", "EMPORIA_PLAYBOOK_PLEDGES",
    "EMPORIA_PLAYBOOK_RUNS",
    "EMPORIA_PLAYBOOK_SELLS", "EMPORIA_PLEDGES_TRUST", "EMPORIA_PUBLISH_VISITS", "EMPORIA_QUEST_DEBRIEFS_TOPIC",
    "EMPORIA_QUEST_FEATS", "EMPORIA_QUEST_IDEAS_TOPIC", "EMPORIA_QUEST_OTHERS",
    "EMPORIA_RELAY_ACCESS", "EMPORIA_RELAY_ENV_FILE", "EMPORIA_RELAY_HOST", "EMPORIA_RELAY_ID",
    "EMPORIA_RELAY_MEMBERS", "EMPORIA_RELAY_PORT", "EMPORIA_RELAY_SIGN_IN_ORIGINS", "EMPORIA_RELAY_URL",
    "EMPORIA_REQUIRE_CHALLENGE",
    "EMPORIA_REQUIRE_NOUS", "EMPORIA_RUN_AS", "EMPORIA_SEASON", "EMPORIA_SEASON_END",
    "EMPORIA_SEASON_START", "EMPORIA_SECRETS_DIR", "EMPORIA_STARTER_LIMITS", "EMPORIA_SESSION_IDLE_HOURS",
    "EMPORIA_SIGNER_DIR", "EMPORIA_SIGNER_OPEN_ALERT", "EMPORIA_SIGNER_OPEN_PER_MIN",
    "EMPORIA_SIGNER_SOCKET", "EMPORIA_SIGNER_TOKEN", "EMPORIA_SIGNER_URL", "EMPORIA_SIGN_LIMITS",
    "EMPORIA_SWEEP_INTERVAL_SECONDS", "EMPORIA_TEMPO_NETWORK", "EMPORIA_TEMPO_RPC_URL",
    "EMPORIA_TEMPO_TOKEN", "EMPORIA_TRANSFER_SEARCH_PAGES", "EMPORIA_TRANSPARENCY_LOG",
    "EMPORIA_TRUSTED_LOCAL_HOSTS", "EMPORIA_TRUST_LOCAL_HEADER_AUTH", "EMPORIA_VISIT_DENY",
    "EMPORIA_VISIT_POLICY", "EMPORIA_VISIT_RELAYS", "EMPORIA_WRITE_REQUIRES_NOUS",
    "EMPORIA_X402_ASSET_EIP155_84532", "EMPORIA_X402_ENABLED", "EMPORIA_X402_EVM_KEY_SOURCE",
    "EMPORIA_X402_EVM_PRIVATE_KEY", "EMPORIA_X402_EXTRA_EIP155_84532",
    "EMPORIA_X402_FACILITATOR_URL", "EMPORIA_X402_MAINNET", "EMPORIA_X402_MAX_DAILY_CENTS",
    "EMPORIA_X402_MAX_PAYMENT_CENTS", "EMPORIA_X402_NETWORKS", "EMPORIA_X402_PAY_NETWORKS",
    "EMPORIA_X402_PHRASE_SECRET", "EMPORIA_X402_PHRASE_SOURCE", "EMPORIA_X402_RECONCILE_BLOCKS",
    "EMPORIA_X402_RPC_EIP155_84532", "EMPORIA_X402_RPC_URL", "EMPORIA_X402_SOLANA_KEY_SOURCE",
    "EMPORIA_X402_SOLANA_PRIVATE_KEY", "EMPORIA_X402_SOLANA_RPC_URL", "FEDERATED_RELAYS",
    "MIN_RATIONALE_CHARS", "OPERATOR_FEE_BPS", "RATE_LIMIT_PER_MINUTE", "SENTRY_DSN",
    "SENTRY_ENVIRONMENT", "SENTRY_TRACES_SAMPLE_RATE", "STRIPE_API_BASE", "STRIPE_API_VERSION",
    "STRIPE_PROFILE_ID", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
)


def network_names() -> list[str]:
    from emporia.networks import NETWORKS
    return [p + re.sub(r"[:-]", "_", net.upper()) for net in NETWORKS for p in NETWORK_PREFIXES]


def key_names() -> list[str]:
    from emporia.identity import RETIRED_ENC_KEYS_VAR
    from emporia.key_source import ENV_KEY_NAMES, SOURCE_VARS, STORAGE_VAR
    return [*ENV_KEY_NAMES.values(), *SOURCE_VARS.values(), STORAGE_VAR, RETIRED_ENC_KEYS_VAR]


def known() -> set[str]:
    """Every name on the lists above (not what a profile holds)."""
    return {*LITERAL_NAMES, *network_names(), *key_names(), *VAULT_NAMES}


def names(values: Mapping[str, str] | None = None) -> list[str]:
    """The names the MCP server is handed: the known ones, the `env:NAME` its key sources
    point at, and every EMPORIA_* or vault name `values` (the profile's .env) holds."""
    values = values or {}
    out = known()
    out |= {m.group(1) for v in values.values() if (m := _ENV_REF.match(str(v)))}
    out |= {k for k in values if k.startswith("EMPORIA_") or k.startswith(VAULT_PREFIXES)}
    return sorted(n for n in out if n not in LITERAL_IN_ENTRY and n not in GATEWAY_ONLY and not n.endswith("_"))


def mcp_env(values: Mapping[str, str] | None = None) -> dict[str, str]:
    """`${NAME}` for each name, and EMPORIA_NO_DOTENV=1: the MCP entry's references."""
    return {**{n: f"${{{n}}}" for n in names(values)}, "EMPORIA_NO_DOTENV": "1"}


def unreferenced(values: Mapping[str, str], entry_env: Mapping[str, str]) -> list[str]:
    """What the profile holds that its MCP entry hands the server neither as a value nor as
    a reference (doctor names it; `emporia agent refresh` adds it)."""
    want = set(names(values)) | {m.group(1) for v in values.values() if (m := _ENV_REF.match(str(v)))}
    have = set(entry_env)
    return sorted(n for n in want if n in values and n not in have and n not in LITERAL_IN_ENTRY
                  and n not in GATEWAY_ONLY)
