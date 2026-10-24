/**
 * Whether an address works only from this machine or this network. The answer comes from the address alone: loopback,
 * private and link-local IP ranges, and host names that only a local network resolves (no dot, or a local suffix).
 * Nothing here knows about any one gateway's host name.
 */
export function isLocalOnlyAddress(address: string): boolean {
  let host: string;
  try {
    host = new URL(address).hostname.toLowerCase();
  } catch {
    return false;
  }
  if (host.startsWith("[") && host.endsWith("]"))
    return isLocalIPv6(host.slice(1, -1));
  const octets = host.split(".");
  if (octets.length === 4 && octets.every((octet) => /^\d{1,3}$/.test(octet))) {
    return isLocalIPv4(octets.map(Number));
  }
  return isLocalName(host);
}

function isLocalIPv4([first, second]: number[]): boolean {
  return (
    first === 0 ||
    first === 10 ||
    first === 127 ||
    (first === 169 && second === 254) ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168)
  );
}

function isLocalIPv6(address: string): boolean {
  return (
    address === "::" ||
    address === "::1" ||
    /^f[cd]/.test(address) || // unique local, fc00::/7
    /^fe[89ab]/.test(address) // link-local, fe80::/10
  );
}

const localSuffixes = [".localhost", ".local", ".lan", ".home.arpa"];

function isLocalName(host: string): boolean {
  return (
    !host.includes(".") ||
    host === "localhost" ||
    localSuffixes.some((suffix) => host.endsWith(suffix))
  );
}
