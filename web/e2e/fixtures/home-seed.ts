import { readFileSync } from "node:fs";
import path from "node:path";
import type { APIRequestContext, Page } from "@playwright/test";
import { fulfilBytes, fulfilJson } from "./intercept";

/*
 * A lived-in household for Home's design captures: real-sounding people, prompts that match their
 * pictures, every kind of work in Recent, and an owner with one real fault. Every value here is DATA
 * a gateway could return; the stubs keep the real responses' shape and only replace their content,
 * and fulfilJson checks each one against the real gateway. Pictures and the voice clip are the
 * household seed's generated files in docs/design/comps/astra/assets.
 */

const ASSETS = path.resolve(__dirname, "../../../docs/design/comps/astra/assets");
const SEED_PATH = "/__seed/";

type Json = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

/** Serves the seed's files to the page under /__seed/. */
export async function serveSeedFiles(page: Page) {
  await page.route(`**${SEED_PATH}*`, (route) => {
    const name = path.basename(new URL(route.request().url()).pathname);
    return fulfilBytes(route, {
      contentType: name.endsWith(".webp") ? "image/webp" : "audio/mpeg",
      body: readFileSync(path.join(ASSETS, name)),
    });
  });
}

interface Offer {
  kind: "chat" | "image" | "speech" | "music" | "video" | "sfx";
  title: string;
  model: string;
  summary: string;
  intent: string;
  seconds: number;
  outputs: string[];
  state?: "ready" | "down" | "off";
}

/** What this household's gateway offers. The names are the operator's data, as a catalog would carry them. */
const OFFERS: Offer[] = [
  { kind: "chat", title: "Qwen 3.6 27B", model: "qwen3.6-27b", summary: "Answers questions and helps you write.", intent: "chat.completions", seconds: 1, outputs: ["text"] },
  { kind: "image", title: "Krea 2 Turbo", model: "krea-2-turbo", summary: "Makes a picture from a description.", intent: "image.generate", seconds: 9, outputs: ["image"] },
  { kind: "speech", title: "Kokoro", model: "kokoro-82m", summary: "Reads your words aloud in a natural voice.", intent: "audio.speech", seconds: 3, outputs: ["audio"] },
  { kind: "music", title: "ACE-Step 1.5", model: "ace-step-1.5", summary: "Makes a short piece of music from a mood.", intent: "audio.music", seconds: 40, outputs: ["audio"] },
];
const OWNER_EXTRA: Offer[] = [
  { kind: "video", title: "LTX 2.5", model: "ltx-2.5", summary: "Makes a short clip from a description.", intent: "video.generate", seconds: 150, outputs: ["video"], state: "down" },
  { kind: "sfx", title: "Stable Audio Open", model: "stable-audio-open", summary: "Makes a sound effect.", intent: "audio.sfx", seconds: 12, outputs: ["audio"], state: "off" },
];
const LABEL: Record<Offer["kind"], string> = {
  chat: "Chat",
  image: "Images",
  speech: "Voice",
  music: "Music",
  video: "Video",
  sfx: "Sound effects",
};

function capabilityRows(template: Json, offers: Offer[], now: number): Json[] {
  return offers.map((offer) => {
    const state = offer.state ?? "ready";
    const status = state === "ready" ? "ready" : "unavailable";
    const reason_code = state === "off" ? "unavailable_unconfigured" : state === "down" ? "unavailable" : "ready";
    return {
      ...template,
      icon_key: offer.kind,
      capability: offer.kind,
      capability_title: LABEL[offer.kind],
      title: offer.title,
      model: offer.model,
      use_name: offer.model,
      recommended_model: offer.model,
      use_names: [offer.model],
      summary: offer.summary,
      intent: offer.intent,
      output_modalities: offer.outputs,
      typical_seconds: offer.seconds,
      cost_hint: { kind: "free", price_usd: null },
      available: state === "ready",
      status,
      reason_code,
      availability: {
        ...(template.availability ?? {}),
        available: state === "ready",
        status,
        reason_code,
        enabled: state !== "off",
        configured: state !== "off",
        up: state === "ready",
        evidence: { source: "health_check", observed_at_s: now - 45 },
      },
    };
  });
}

/** Successful requests, so the chooser orders this person's usual work first. */
function pastReceipts(now: number, uses: Array<[Offer["kind"], number]>): Json[] {
  const path: Record<Offer["kind"], string> = {
    chat: "/gateway/v1/chat/completions",
    image: "/gateway/v1/images/generations",
    speech: "/gateway/v1/audio/speech",
    music: "/gateway/v1/media/jobs/music",
    video: "/gateway/v1/videos",
    sfx: "/gateway/v1/audio/sfx",
  };
  return uses.flatMap(([kind, count], group) =>
    Array.from({ length: count }, (_, index) => ({
      request_id: `seed-${kind}-${index}`,
      intent: OFFERS.concat(OWNER_EXTRA).find((offer) => offer.kind === kind)!.intent,
      path: path[kind],
      status: "succeeded",
      accepted_at_ms: (now - 3600 * (group * 5 + index + 4)) * 1000,
    })),
  );
}

function apps(template: Json | undefined, names: Array<[string, number]>, now: number): Json[] {
  if (!template) return [];
  return names.map(([name, ago], index) => ({
    ...template,
    id: `seed-app-${index}`,
    name,
    disabled_at: null,
    credentials: (template.credentials?.length ? template.credentials : [{}]).slice(0, 1).map((key: Json) => ({
      ...key,
      last_used_at: now - ago,
    })),
  }));
}

export interface Persona {
  name: string;
  ownerName: string;
}

async function stubSession(page: Page, request: APIRequestContext, persona: Persona) {
  const real = await (await request.get("/me/api/session")).json();
  await page.route("**/me/api/session", (route) =>
    fulfilJson(route, { ...real, user: { ...real.user, name: persona.name } }),
  );
}

/** The one-time request history notice is due for this person (the History journey makes the real case). */
export async function seedHistoryNoticeDue(page: Page, request: APIRequestContext) {
  const real = await (await request.get("/me/api/retention")).json();
  await page.route("**/me/api/retention", (route) =>
    fulfilJson(route, { ...real, keep: false, notice_due: true, ...("household_off" in real ? { household_off: false } : {}) }),
  );
}

/** Robin, a member with a week of work: a picture being made (started from Home in this tab), pictures, a voice clip and a chat. */
export async function seedReturningMember(page: Page, request: APIRequestContext) {
  const persona = { name: "Robin Okafor", ownerName: "Sam" };
  await stubSession(page, request, persona);
  await serveSeedFiles(page);
  const real = await (await request.get("/me/api/home")).json();
  const now = Date.now() / 1000;
  // Robin pressed Make on Home in this tab a moment before the running picture was accepted (handoff.ts).
  await page.addInitScript((at) => {
    sessionStorage.setItem(
      "azizam.home.made",
      JSON.stringify([{ capability: "image", prompt: "A watercolour of the garden shed in spring rain", at }]),
    );
  }, now - 5);
  const template = real.capabilities.find((row: Json) => row.icon_key === "chat") ?? real.capabilities[0];
  await page.route("**/me/api/home", (route) =>
    fulfilJson(route, {
      ...real,
      user: { ...real.user, name: persona.name },
      gateway: { ...(real.gateway ?? {}), owner_name: persona.ownerName },
      capabilities: capabilityRows(template, OFFERS, now),
      apps: apps(real.apps?.[0], [["Open WebUI", 7200], ["Daisy", 90_000]], now),
      receipts: [
        { request_id: "seed-making", intent: "image.generate", path: "/gateway/v1/media/jobs/image-generations", status: "running", accepted_at_ms: (now - 3) * 1000 },
        ...pastReceipts(now, [["image", 4], ["chat", 3], ["speech", 1]]),
      ],
      recent_chats: [
        { request_id: "seed-chat", record_id: "seed-chat-record", created_at: now - 3 * 3600, title: "Make a watering schedule for the tomatoes and herbs while we’re away for a week" },
      ],
    }),
  );
  await page.route("**/me/api/media?limit=8", (route) =>
    fulfilJson(route, {
      assets: [
        { asset_id: "seed-bed", kind: "image", prompt: "A raised garden bed with tomatoes and marigolds, late afternoon light", download_url: `${SEED_PATH}32c53e86c9a1.webp`, created_at: now - 40 * 60 },
        { asset_id: "seed-story", kind: "audio", prompt: "Bedtime story: the fox who carried a lantern", download_url: `${SEED_PATH}2aece6ad2150.bin`, media_type: "audio/mpeg", size_bytes: 24_398, created_at: now - 2 * 3600 },
        { asset_id: "seed-birdhouse", kind: "image", prompt: "A blue birdhouse hanging in an apple tree, morning light", download_url: `${SEED_PATH}17cdc5827979.webp`, created_at: now - 26 * 3600 },
        { asset_id: "seed-dog", kind: "image", prompt: "Our golden retriever asleep on the window seat", download_url: `${SEED_PATH}024ba27b2608.webp`, created_at: now - 50 * 3600 },
        { asset_id: "seed-tart", kind: "image", prompt: "Apple tart cooling on a rack, flour on the table", download_url: `${SEED_PATH}d08ae7428378.webp`, created_at: now - 74 * 3600 },
      ],
      has_more: true,
      next_cursor: null,
    }),
  );
}

/** June, a member on her first visit: everything she may use, and no work yet. */
export async function seedFirstVisitMember(page: Page, request: APIRequestContext) {
  const persona = { name: "June Park", ownerName: "Sam" };
  await stubSession(page, request, persona);
  const real = await (await request.get("/me/api/home")).json();
  const now = Date.now() / 1000;
  const template = real.capabilities.find((row: Json) => row.icon_key === "chat") ?? real.capabilities[0];
  await page.route("**/me/api/home", (route) =>
    fulfilJson(route, {
      ...real,
      user: { ...real.user, name: persona.name },
      gateway: { ...(real.gateway ?? {}), owner_name: persona.ownerName },
      capabilities: capabilityRows(template, OFFERS, now),
      apps: apps(real.apps?.[0], [["Open WebUI", 0]], now).map((app) => ({ ...app, credentials: [] })),
      receipts: [],
      recent_chats: [],
    }),
  );
  await page.route("**/me/api/media?limit=8", (route) => fulfilJson(route, { assets: [], has_more: false, next_cursor: null }));
}

/** Sam, the owner, while the video engine refuses connections and sound effects are not set up. */
export async function seedOwner(page: Page, request: APIRequestContext) {
  const persona = { name: "Sam Okafor", ownerName: "Sam" };
  await stubSession(page, request, persona);
  await serveSeedFiles(page);
  const real = await (await request.get("/me/api/home")).json();
  const status = await (await request.get("/admin/api/status")).json();
  const now = Date.now() / 1000;
  const template = real.capabilities.find((row: Json) => row.icon_key === "chat") ?? real.capabilities[0];
  await page.route("**/me/api/home", (route) =>
    fulfilJson(route, {
      ...real,
      user: { ...real.user, name: persona.name },
      gateway: { ...(real.gateway ?? {}), owner_name: persona.ownerName },
      capabilities: capabilityRows(template, [...OFFERS, ...OWNER_EXTRA], now),
      apps: apps(real.apps?.[0], [["Open WebUI", 1800], ["Daisy", 5400], ["Claude Code", 86_400]], now),
      receipts: pastReceipts(now, [["chat", 5], ["image", 3]]),
      recent_chats: [
        { request_id: "seed-owner-chat", record_id: "seed-owner-chat-record", created_at: now - 2 * 3600, title: "Plan a two-day coastal trip by train, with a bike-friendly route and a picnic stop" },
      ],
    }),
  );
  // Per-key trend maps are keyed by app names earlier cases created, which the shape oracle cannot know;
  // the panel reads none of them, so the stub sends them empty.
  const trends = status.status?.metrics?.trends;
  if (trends)
    for (const key of Object.keys(trends)) if (key.startsWith("per_key_")) trends[key] = {};
  const health = status.status?.current_health;
  if (health) {
    const video = {
      service_id: "seed-video-studio",
      name: "Video studio",
      capability_title: "Video",
      capability_labels: ["Video"],
      icon_key: "video",
      configured: true,
      enabled: true,
      in_use: true,
      role: "primary",
      routable: false,
      observation: "down",
      tone: "bad",
      last_error: "connection refused",
    };
    await page.route("**/admin/api/status", (route) =>
      fulfilJson(route, {
        ...status,
        status: {
          ...status.status,
          current_health: {
            ...health,
            observation_available: true,
            services: [...(health.services ?? []).filter((service: Json) => !service.capability_labels?.includes("Video")), video],
          },
        },
      }),
    );
  }
  await page.route("**/me/api/media?limit=8", (route) =>
    fulfilJson(route, {
      assets: [
        { asset_id: "seed-owner-tart", kind: "image", prompt: "Apple tart cooling on a rack, flour on the table", download_url: `${SEED_PATH}d08ae7428378.webp`, created_at: now - 40 * 60 },
        { asset_id: "seed-owner-lake", kind: "image", prompt: "A misty lake at dawn with a cabin and a canoe", download_url: `${SEED_PATH}cdf1290576bb.webp`, created_at: now - 26 * 3600 },
        { asset_id: "seed-owner-party", kind: "image", prompt: "Backyard birthday table under string lights", download_url: `${SEED_PATH}f1ea95783d71.webp`, created_at: now - 4 * 86_400 },
      ],
      has_more: true,
      next_cursor: null,
    }),
  );
}
