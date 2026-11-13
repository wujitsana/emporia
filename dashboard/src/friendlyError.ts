/** A network failure as a sentence, not "TypeError: Failed to fetch"; a script error as a plain sentence; any other message as written. */
export function friendlyError(error: string): string {
  // "Error: 500 /payments/records?mine=1": the status in words, never the request path.
  const status = /^(?:Error:\s*)?(\d{3})\s+\//.exec(error)?.[1];
  if (status) return `The relay answered with an error (${status}). Try again in a moment.`;
  // A class name from the transport ("GatewayReauthRequiredError: ...") is no sentence for a person.
  if (/^[A-Za-z]*Error:/.test(error) && !/^(?:Type)?Error:/.test(error)) return "Something went wrong loading this. Try reopening the page.";
  if (/failed to fetch|networkerror|load failed|network request failed/i.test(error)) return "Can't reach the relay. Try again in a moment.";
  // A JS "TypeError: x is undefined" / "Error: ..." is a bug's message, not something to show a person.
  if (/^(?:Type)?Error:/.test(error)) return "Something went wrong; try again.";
  return error;
}
