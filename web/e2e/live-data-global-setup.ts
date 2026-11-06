import { request, type FullConfig } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { widenStepUpWindow } from "./fixtures/session-policy";

const ownerEmail = "owner-live-data@example.test";
const memberEmail = "member-live-data@example.test";
const password = "Live data browser password 2026";

async function login(baseURL: string, email: string, path: string, owner = false) {
  const api = await request.newContext({ baseURL });
  const response = await api.post("/auth/api/login", {
    data: { email, password },
  });
  if (!response.ok()) {
    throw new Error(`Login for ${email} returned HTTP ${response.status()}`);
  }
  // The saved owner sign-in outlives the 15-minute step-up window
  // (fixtures/session-policy.ts); the member has no risky actions.
  if (owner) await widenStepUpWindow(api);
  await api.storageState({ path });
  await api.dispose();
}

export default async function setup(config: FullConfig) {
  await mkdir("test-results", { recursive: true });
  const baseURL = config.projects[0].use.baseURL;
  if (typeof baseURL !== "string") {
    throw new Error("live-data tests require baseURL");
  }
  await login(baseURL, ownerEmail, "test-results/live-data-owner.json", true);
  await login(baseURL, memberEmail, "test-results/live-data-member.json");
}
