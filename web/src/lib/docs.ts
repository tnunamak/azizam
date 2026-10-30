/**
 * Where the public setup guides live. Every in-app link to client setup is built from this one value. The web app is
 * a static export, so an operator who hosts the guides elsewhere sets NEXT_PUBLIC_DOCS_BASE at build time.
 */
export const DOCS_BASE = (
  process.env.NEXT_PUBLIC_DOCS_BASE || "https://azizam.dev/docs"
).replace(/\/+$/, "");

/** One client's setup page: `/docs/connect/<client-id>/`, with the ids from lib/app-guides.json. */
export function clientDocsUrl(clientId: string): string {
  return `${DOCS_BASE}/connect/${encodeURIComponent(clientId)}/`;
}

/** The public MCP setup page: `/docs/api/mcp/`. */
export const MCP_DOCS_URL = `${DOCS_BASE}/api/mcp/`;
