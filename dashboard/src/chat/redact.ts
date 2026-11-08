/**
 * Defence in depth for the chat: nothing key- or token-shaped is drawn.
 *
 * Hermes already redacts approval commands, and Emporia's key_guard hides key
 * assignments in tool results before the model sees them. Tool output shown in
 * the chat comes from Hermes's own event stream, so the same rules run again
 * here: key assignments (key_guard's names), a shown recovery phrase, and
 * values that look like a private key or a bearer token.
 */

const SECRET_NAME =
  "(EMPORIA_(?:IDENTITY|ENCRYPTION|X402_EVM|X402_SOLANA|OPERATOR)_PRIVATE_KEY|EMPORIA_X402_PHRASE_SECRET" +
  "|EMPORIA_ENCRYPTION_RETIRED_KEYS|EMPORIA_SIGNER_TOKEN|OP_SERVICE_ACCOUNT_TOKEN|BW_SESSION" +
  "|VAULT_TOKEN|DOPPLER_TOKEN|INFISICAL_TOKEN|AZURE_CLIENT_SECRET|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN" +
  "|STRIPE_SECRET_KEY|STRIPE_WEBHOOK_SECRET|[A-Z][A-Z0-9_]*(?:API_KEY|_TOKEN|_SECRET|PRIVATE_KEY|PASSWORD))";

const ASSIGNMENT = new RegExp(
  "\\b" + SECRET_NAME + "(['\"]?)\\s*[=:]\\s*(?:'[^']*'|\"[^\"]*\"|[^\\s\"',}]+)",
  "g",
);
const PHRASE_BLOCK = /──── RECOVERY PHRASE ────[\s\S]*?(?:──── END OF RECOVERY PHRASE ────|$)/g;
const PATTERNS: RegExp[] = [
  /\b0x[0-9a-fA-F]{64}\b/g, // a 32-byte hex private key
  /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g, // a JWT
  /\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b/g, // payment-provider keys
  /\bsk-[A-Za-z0-9_-]{20,}\b/g, // model-provider keys
  /\b(?:ghp|gho|ghs|xox[abp])[-_][A-Za-z0-9-]{16,}\b/g, // forge / chat tokens
  /\b[1-9A-HJ-NP-Za-km-z]{86,90}\b/g, // a base58 64-byte secret key
  /([?&](?:token|ticket|key|secret|internal)=)[^&\s"']+/gi, // credentials in a URL
  /\b(Bearer)\s+[A-Za-z0-9._~+/-]{12,}=*/g,
];

export const HIDDEN = "[hidden]";

export function redactSecrets(text: string): string {
  if (!text) return text;
  let out = text.replace(PHRASE_BLOCK, "[recovery phrase hidden]");
  out = out.replace(ASSIGNMENT, (_m, name: string) => `${name}=${HIDDEN}`);
  for (const re of PATTERNS) {
    out = out.replace(re, (m, lead?: string) =>
      typeof lead === "string" && /^[?&]|Bearer/.test(lead) ? `${lead}${lead === "Bearer" ? " " : ""}${HIDDEN}` : HIDDEN,
    );
  }
  return out;
}
