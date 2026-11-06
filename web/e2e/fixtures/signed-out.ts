import type { Browser, Route } from "@playwright/test";
import { fulfilJson } from "./intercept";

/**
 * Answers the sign-in options for the request's own return target, as the gateway does. The fixture has no identity
 * provider, so the sign-in methods are answered here, as the OIDC case in root-paths.spec.ts does.
 */
export function answerSignInOptions(route: Route, overrides: Record<string, unknown> = {}) {
  const returnTo = new URL(route.request().url()).searchParams.get("return_to") ?? "/";
  return fulfilJson(route, {
    built_in: false,
    oidc: true,
    oidc_label: "Okafor ID",
    oidc_url: `/login?return_to=${encodeURIComponent(returnTo)}`,
    instance_name: "Okafor household",
    owner_password: true,
    fresh_sign_in: false,
    ...overrides,
  });
}

/** A browser with no session, at one size: the signed-out front door. */
export async function signedOut(
  browser: Browser,
  baseURL: string | undefined,
  viewport: { width: number; height: number },
) {
  const context = await browser.newContext({ baseURL, viewport, storageState: { cookies: [], origins: [] } });
  return { context, page: await context.newPage() };
}
