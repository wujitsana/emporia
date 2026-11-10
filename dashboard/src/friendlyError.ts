/** A network failure as a sentence, not "TypeError: Failed to fetch"; any other message as written. */
export function friendlyError(error: string): string {
  // "Error: 500 /payments/records?mine=1": the status in words, never the request path.
  const status = /^(?:Error:\s*)?(\d{3})\s+\//.exec(error)?.[1];
  if (status) return `The relay answered with an error (${status}). Try again in a moment.`;
  return /failed to fetch|networkerror|load failed|network request failed/i.test(error)
    ? "Can't reach the relay. Try again in a moment."
    : error;
}
