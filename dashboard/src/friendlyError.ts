/** A network failure as a sentence, not "TypeError: Failed to fetch"; any other message as written. */
export function friendlyError(error: string): string {
  return /failed to fetch|networkerror|load failed|network request failed/i.test(error)
    ? "Can't reach the relay. Try again in a moment."
    : error;
}
