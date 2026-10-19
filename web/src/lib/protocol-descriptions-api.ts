import { adminFetch } from "@/lib/api";

export interface InstalledProtocolDescription {
  id: string;
  title: string;
  executor: string;
  operations: string[];
  source: "seed" | "user";
  dispatch_supported: boolean;
  validation: { valid: true; message: string };
}

export interface ProtocolDescriptionCatalog {
  protocols: InstalledProtocolDescription[];
  problems: { file: string; reason: string }[];
}

export const protocolDescriptionsApi = {
  list: () =>
    adminFetch<ProtocolDescriptionCatalog>("/admin/api/engine-protocols"),
};
