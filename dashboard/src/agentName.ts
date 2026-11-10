/**
 * How an agent's name is shown. On the relay you are looking at, an agent is its bare name
 * ("ali"), never "ali@127.0.0.1:18741". A visitor from another relay is "name@host", with the
 * host only and never a port. The relay's own host comes from /health (`setOwnHost`); until
 * it is known, an address keeps its host (minus the port) rather than guess.
 */
let ownHost = "";

/** Record this relay's own `host` (from /health); an address on it is a local agent. */
export function setOwnHost(host: string | null | undefined): void {
  ownHost = (host ?? "").trim().toLowerCase();
}

function dropPort(host: string): string {
  if (host.startsWith("[")) return host.slice(0, host.indexOf("]") + 1) || host;
  return host.replace(/:\d+$/, "");
}

/** Whether `name@host[:port]` names an agent of the relay being viewed. */
function isLocalHost(host: string): boolean {
  const h = host.toLowerCase();
  return ownHost !== "" && ((h === ownHost || (dropPort(h) === dropPort(ownHost) && !/:\d+$/.test(h))));
}

/** "ali" for a local agent, "name@host" for a visitor; never a port. Plain ids pass through. */
export function bareAgentName(idOrAddress: string): string {
  const at = idOrAddress.indexOf("@");
  if (at < 0) return idOrAddress;
  const name = idOrAddress.slice(0, at);
  const host = idOrAddress.slice(at + 1);
  if (isLocalHost(host)) return name;
  return `${name}@${dropPort(host)}`;
}

/** Whether an address is a visitor from another relay (it carries a host that is not this relay's). */
export function isVisitorAddress(idOrAddress: string): boolean {
  const at = idOrAddress.indexOf("@");
  return at >= 0 && !isLocalHost(idOrAddress.slice(at + 1));
}
