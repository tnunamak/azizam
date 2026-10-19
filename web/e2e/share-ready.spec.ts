import { request as playwrightRequest } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";

// A crawler sends no cookie and no key. Everything here is asked as a stranger.
async function stranger(baseURL: string | undefined) {
  return playwrightRequest.newContext({ baseURL, storageState: { cookies: [], origins: [] } });
}

test("robots.txt is public and asks every crawler to stay away", async ({ baseURL }) => {
  const api = await stranger(baseURL);
  const response = await api.get("/robots.txt");
  expect(response.status()).toBe(200);
  expect(response.headers()["content-type"]).toMatch(/^text\/plain/);
  const lines = (await response.text()).split("\n").map((line) => line.trim());
  expect(lines).toContain("User-agent: *");
  expect(lines).toContain("Disallow: /");
  await api.dispose();
});

test("every page the gateway serves says noindex, in the header and in the page", async ({ baseURL, page }) => {
  const api = await stranger(baseURL);
  for (const path of ["/", "/sign-in/", "/invite/", "/setup/"]) {
    const response = await api.get(path, { headers: { accept: "text/html" } });
    expect(response.status(), path).toBe(200);
    expect(response.headers()["x-robots-tag"], path).toBe("noindex, nofollow");
  }
  const missing = await api.get("/no-such-page", { headers: { accept: "text/html" } });
  expect(missing.status()).toBe(404);
  expect(missing.headers()["x-robots-tag"]).toBe("noindex, nofollow");
  await api.dispose();
  await page.goto("/sign-in/");
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute("content", /noindex/);
  await expect(page.locator('meta[name="robots"]')).toHaveAttribute("content", /nofollow/);
});

test("data and images are not tagged", async ({ baseURL }) => {
  const api = await stranger(baseURL);
  const icon = await api.get("/icon.svg");
  expect(icon.headers()["x-robots-tag"]).toBeUndefined();
  const health = await api.get("/health");
  expect(health.headers()["x-robots-tag"]).toBeUndefined();
  await api.dispose();
});
