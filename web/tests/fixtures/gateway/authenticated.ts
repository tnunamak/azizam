import { readFile } from "node:fs/promises";
import type { Browser, BrowserContext } from "@playwright/test";

type Bootstrap = {
  base_url: string;
  cookie_name: string;
  sessions: Record<string, { cookie: string; csrf: string }>;
};

export async function authenticatedContext(
  browser: Browser,
  bootstrapPath: string,
  identity: "admin" | "user_a" | "user_b",
): Promise<{ context: BrowserContext; baseUrl: string; csrf: string }> {
  const bootstrap = JSON.parse(await readFile(bootstrapPath, "utf8")) as Bootstrap;
  const url = new URL(bootstrap.base_url);
  const session = bootstrap.sessions[identity];
  const context = await browser.newContext();
  await context.addCookies([{
    name: bootstrap.cookie_name,
    value: session.cookie,
    domain: url.hostname,
    path: "/",
    httpOnly: true,
    secure: false,
    sameSite: "Lax",
  }]);
  return { context, baseUrl: bootstrap.base_url, csrf: session.csrf };
}
