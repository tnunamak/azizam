import { adminFetch, type AdministratorsOnRecord } from "./api";

/** An address an owner trusts to use one App with no key. Read by the Apps page, Connect and the App's dialog. */
export interface TrustedAddress {
  id: string;
  app_id: string;
  address: string;
  note: string | null;
  created_at: number;
  /** Null when the address was made before its creator was recorded. */
  created_by: string | null;
  /** Present only when the cut-over would remove the address: keep it, or add it again. */
  cutover?: "keep" | "readd";
  /** The administrators on record who could also have added it. Keeping it asks the owner to confirm them first. */
  administrators?: AdministratorsOnRecord;
  /** The person recorded as its maker, when that is another person. */
  made_by_name?: string;
}

export const trustedAddressesPath = (appId: string) =>
  `/admin/api/apps/${encodeURIComponent(appId)}/trusted-addresses`;

/** One app's trusted addresses. The App's section on the Apps page, its dialog and Connect share this cache entry. */
export const trustedAddressesKey = (appId: string) => [
  "admin",
  "app-trusted-addresses",
  appId,
];

export function listTrustedAddresses(appId: string): Promise<TrustedAddress[]> {
  return adminFetch<{ trusted_addresses: TrustedAddress[] }>(
    trustedAddressesPath(appId),
  ).then((r) => r.trusted_addresses);
}
