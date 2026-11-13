/** The relay answered a signed call from the viewed agent with 401: it has no such agent. Anything else (403, a
 *  network failure, a server error) says nothing about registration. `api.ts` throws "<status> <path>". */
export function isNotRegistered(e: unknown): boolean {
  return /^401\b/.test(String((e as { message?: unknown } | null)?.message ?? e ?? ""));
}
