/**
 * Who can reach an address, from the address alone. "loopback": only this machine (127.0.0.0/8, ::1, localhost).
 * "lan": other devices on this network (private, link-local and local-network addresses and names). "public": anything
 * else, and text that is not an absolute address. Nothing here knows about any one gateway's host name.
 */
export type AddressReach = "loopback" | "lan" | "public";

export function addressReach(address: string): AddressReach {
  let host: string;
  try {
    host = new URL(address).hostname.toLowerCase();
  } catch {
    return "public";
  }
  if (host.startsWith("[") && host.endsWith("]"))
    return ipv6Reach(host.slice(1, -1));
  const octets = host.split(".");
  if (octets.length === 4 && octets.every((octet) => /^\d{1,3}$/.test(octet))) {
    return ipv4Reach(octets.map(Number));
  }
  return nameReach(host);
}

/** True when only this machine or this network can reach the address. */
export function isLocalOnlyAddress(address: string): boolean {
  return addressReach(address) !== "public";
}

function ipv4Reach([first, second]: number[]): AddressReach {
  if (first === 127) return "loopback";
  const local =
    first === 0 ||
    first === 10 ||
    (first === 169 && second === 254) ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168);
  return local ? "lan" : "public";
}

function ipv6Reach(address: string): AddressReach {
  const embedded = embeddedIPv4(address);
  if (embedded) return ipv4Reach(embedded);
  if (address === "::1") return "loopback";
  const local =
    address === "::" ||
    /^f[cd]/.test(address) || // unique local, fc00::/7
    /^fe[89ab]/.test(address); // link-local, fe80::/10
  return local ? "lan" : "public";
}

/**
 * The IPv4 address inside an IPv4-mapped IPv6 address (::ffff:a.b.c.d), or null. WHATWG URLs write it in hex
 * (`[::ffff:c0a8:105]` for 192.168.1.5), so both spellings are read.
 */
function embeddedIPv4(address: string): number[] | null {
  const dotted = /^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/.exec(address);
  if (dotted) return dotted[1].split(".").map(Number);
  const hex = /^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/.exec(address);
  if (!hex) return null;
  const high = parseInt(hex[1], 16);
  const low = parseInt(hex[2], 16);
  return [high >> 8, high & 0xff, low >> 8, low & 0xff];
}

const localSuffixes = [".local", ".lan", ".home.arpa"];

function nameReach(host: string): AddressReach {
  if (host === "localhost" || host.endsWith(".localhost")) return "loopback";
  const local =
    !host.includes(".") || localSuffixes.some((suffix) => host.endsWith(suffix));
  return local ? "lan" : "public";
}
