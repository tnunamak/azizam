// Global setup for the quality sweep: signs in the owner and a member, and
// records what the states need (a picture workflow, an invitation link).
import { request } from "@playwright/test";
import { existsSync } from "node:fs";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { sweepPaths, sweepTarget } from "./target";

interface Seed {
  base_url: string;
  owner_email: string;
  member_email: string;
  password: string;
  now?: number;
}

async function waitForFile(file: string, timeoutMs = 60_000) {
  const until = Date.now() + timeoutMs;
  while (!existsSync(file)) {
    if (Date.now() > until) throw new Error(`Sweep seed manifest never appeared: ${file}`);
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

async function signIn(base: string, email: string, password: string, file: string) {
  const api = await request.newContext({ baseURL: base });
  const response = await api.post("/auth/api/login", { data: { email, password } });
  if (!response.ok()) throw new Error(`Sweep sign-in for ${email} returned HTTP ${response.status()}`);
  await api.storageState({ path: file });
  return api;
}

type Workflow = {
  id: string;
  title?: string;
  availability?: { available?: boolean };
  inputs?: Array<Media>;
  outputs?: Array<Media | string>;
};
type Media = { kind?: string; type?: string; media_type?: string; accept?: string | string[] };

const isPicture = (item?: Media | string) => {
  if (!item) return false;
  if (typeof item === "string") return item.startsWith("image/") || item === "image";
  const accept = Array.isArray(item.accept) ? item.accept.join(",") : item.accept ?? "";
  return item.kind === "image" || item.type === "image" || !!item.media_type?.startsWith("image/") || accept.includes("image/");
};

export default async function setup() {
  const target = sweepTarget();
  const paths = sweepPaths(target);
  await mkdir(paths.dir, { recursive: true });
  // SWEEP_KEEP=1 reruns part of the matrix and keeps the other results (same-named files are replaced).
  if (process.env.SWEEP_KEEP !== "1") await rm(paths.findings, { recursive: true, force: true });
  await mkdir(paths.findings, { recursive: true });
  await mkdir(paths.shots, { recursive: true });
  await waitForFile(paths.seedManifest);
  const seed = JSON.parse(await readFile(paths.seedManifest, "utf8")) as Seed;
  const base = target.baseURL;
  const owner = await signIn(base, seed.owner_email, seed.password, paths.ownerState);
  const member = await signIn(base, seed.member_email, seed.password, paths.memberState);
  await member.dispose();
  await writeFile(paths.anonymousState, JSON.stringify({ cookies: [], origins: [] }));

  // A picture workflow for the Try workflow form.
  let pictureWorkflow: string | undefined;
  const workflows = await owner.get("/me/api/workflows");
  if (workflows.ok()) {
    const body = await workflows.json();
    const list: Workflow[] = Array.isArray(body) ? body : body.workflows ?? body.data ?? [];
    // A workflow that makes a picture; one that also takes a picture in is the harder form.
    const makes = list.filter((w) => w.outputs?.some(isPicture));
    const usable = (w: Workflow) => w.availability?.available !== false;
    const ranked = [
      ...makes.filter((w) => usable(w) && w.inputs?.some(isPicture)),
      ...makes.filter(usable),
      ...makes,
    ];
    pictureWorkflow = ranked[0]?.id;
  }

  // An invitation link. The household fixture seeds deterministic tokens;
  // elsewhere the sweep creates one in the disposable harness data.
  let inviteToken: string | undefined;
  if (target.name === "fixtures") {
    inviteToken = "household-fixture-invite-june@example.test";
  } else {
    const session = await (await owner.get("/admin/api/session")).json();
    const csrf = session?.actor?.csrf_token;
    const listing = await owner.get("/admin/api/profiles");
    const profiles = listing.ok() ? (await listing.json()).profiles ?? [] : [];
    if (csrf && profiles[0]?.id) {
      const created = await owner.post("/admin/api/invites", {
        headers: { "x-azizam-csrf": csrf, "x-azizam-admin-action": "1" },
        data: { profile_id: profiles[0].id, email: "sweep-invitee@example.test", expires_in: 86_400 },
      });
      if (created.ok()) {
        const { url } = await created.json();
        inviteToken = new URLSearchParams(String(url).split("#")[1] ?? "").get("token") ?? undefined;
      }
    }
  }
  await owner.dispose();

  await writeFile(
    paths.context,
    JSON.stringify(
      {
        target: target.name,
        base,
        setupBase: target.setupBaseURL,
        now: seed.now,
        pictureWorkflow,
        inviteToken,
        startedAt: new Date().toISOString(),
      },
      null,
      2,
    ),
  );
}
