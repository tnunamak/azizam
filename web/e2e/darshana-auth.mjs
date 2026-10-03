import path from "node:path";
import { mkdir } from "node:fs/promises";

// Credentials belong only to the isolated UI harness, never the live gateway.
export default async function login(browser) {
  const baseURL = process.env.APP_URL ?? "http://127.0.0.1:18180";
  const url = new URL(baseURL);
  if (
    url.hostname !== "127.0.0.1" ||
    Number(url.port) < 18000 ||
    Number(url.port) > 18998
  ) {
    throw new Error("Darshana review requires an isolated UI harness port");
  }
  const context = await browser.newContext({ baseURL });
  try {
    const response = await context.request.post("/auth/api/login", {
      data: {
        email: "owner@example.test",
        password: "Portable UI test password 2026",
      },
    });
    if (!response.ok())
      throw new Error(`Seeded owner login returned HTTP ${response.status()}`);
    const session = await context.request.get("/admin/api/session");
    if (!session.ok() || !(await session.json()).actor)
      throw new Error("Seeded owner session was not established");
    const file = path.resolve(`test-results/darshana-auth-${url.port}.json`);
    await mkdir(path.dirname(file), { recursive: true });
    await context.storageState({ path: file });
    return file;
  } finally {
    await context.close();
  }
}
