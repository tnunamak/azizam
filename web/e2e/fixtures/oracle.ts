// The structural oracle for endpoints a read-only probe cannot reach: those that
// change state (invites, people, keys, jobs, generations) or need data the browser
// seed lacks (media, a receipt, an active category policy).
//
// Once per Playwright run, global-setup starts a disposable gateway of its own
// (the household fixture: scripts/ui/serve.py with UI_SEED_PROFILE=household,
// beside the fake engine), calls every such endpoint once for real with
// requests that succeed, and writes each response body to one file keyed by
// the registry's endpoint id. fulfilJson derives a stub's expected structure
// from that body, the way it does for a read probe. Nothing here is hand-written
// structure: a field the gateway renames changes the file and fails the stub.
//
// The server is thrown away afterwards, so no call can touch a server that a
// browser test shares. Ports: the oracle gateway uses the run's port - 10 and
// the fake engine port - 9. scripts/ci-local gives each gate seven blocks of ten
// ports and starts the main browser run on the second, so the block before it is free.
import { request, type APIRequestContext } from "@playwright/test";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, openSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";

// Gateway response bodies are read by key and not typed one by one.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = Record<string, any>;

export interface OracleFile {
  // Endpoint id (the registry's `GET /me/api/media/{id}`) to the real response body.
  bodies: Record<string, unknown>;
}

const WEB = path.resolve(__dirname, "../..");

// A short silent WAV, the upload for the transcription route.
function silentWav(): Buffer {
  const samples = 1600;
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + samples * 2, 4);
  header.write("WAVEfmt ", 8);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(16000, 24);
  header.writeUInt32LE(32000, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(samples * 2, 40);
  return Buffer.concat([header, Buffer.alloc(samples * 2)]);
}

// A policy snapshot the category store accepts (the same shape the categories
// specs seed through the real API).
function categorySnapshot() {
  const grant = (capability: string) => ({ capability, service: "local-service@r1" });
  return {
    id: "oracle-snapshot",
    categories: {
      location: {
        id: "location",
        label: "Data location",
        kind: "set",
        values: ["local", "remote"],
        applies_to: ["release"],
      },
    },
    people: {
      Casey: [
        {
          id: "person-access",
          grants: [grant("chat")],
          require: [{ id: "known-location", category: "location", op: "exists", values: [] }],
          deny: [],
          missing: {},
          mode: "v3",
          legacy_tags: [],
        },
      ],
    },
    apps: {
      Notebook: [
        { id: "app-access", grants: [grant("chat")], require: [], deny: [], missing: {}, mode: "v3", legacy_tags: [] },
      ],
    },
    services: {
      "local-service@r1": {
        id: "local-service@r1",
        checkable: true,
        dependencies: [],
        members: [
          {
            id: "local-model@r1",
            kind: "release",
            assertions: [
              {
                category: "location",
                value: "local",
                source: "Oracle fixture",
                accepted: true,
                observed_at: "2026-01-01T00:00:00Z",
                accepted_at: "2026-01-01T00:00:00Z",
                accepting_owner: "Casey",
                mapping_version: "fixture-1",
              },
            ],
          },
        ],
      },
    },
    retained: {},
  };
}

// Repeats a read until the answer satisfies `done`; the fake engine finishes in
// moments, so the deadline only turns a stuck job into a named failure.
async function settle<T>(read: () => Promise<T>, done: (value: T) => boolean, label: string): Promise<T> {
  const deadline = Date.now() + 30_000;
  for (;;) {
    const value = await read();
    if (done(value)) return value;
    if (Date.now() > deadline) throw new Error(`oracle: ${label} did not finish`);
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

type Reply = { status: number; body: Json };

class Session {
  constructor(
    readonly api: APIRequestContext,
    private readonly headers: Record<string, string> = {},
  ) {}

  async call(
    method: string,
    url: string,
    options: { data?: unknown; multipart?: Json; headers?: Record<string, string> } = {},
  ): Promise<Reply> {
    const response = await this.api.fetch(url, {
      method,
      ...(options.data === undefined ? {} : { data: options.data }),
      ...(options.multipart ? { multipart: options.multipart } : {}),
      headers: { ...this.headers, ...options.headers },
    });
    const text = await response.text();
    let body: unknown = text;
    try {
      body = text ? JSON.parse(text) : {};
    } catch {
      // Not JSON: the caller decides whether that matters.
    }
    return { status: response.status(), body: body as Json };
  }
}

export async function exerciseOracle(origin: string, manifest: Json, engineOrigin: string): Promise<OracleFile> {
  const bodies: Record<string, unknown> = {};
  const contexts: APIRequestContext[] = [];
  const open = async () => {
    const context = await request.newContext({ baseURL: origin });
    contexts.push(context);
    return context;
  };
  // Records the body of a call under the endpoint id, after checking it succeeded.
  const record = (id: string, result: Reply, expected: number[] = [200, 201, 202]) => {
    if (!expected.includes(result.status))
      throw new Error(`oracle: ${id} returned HTTP ${result.status}: ${JSON.stringify(result.body).slice(0, 300)}`);
    bodies[id] = result.body;
    return result.body;
  };

  try {
    const owner = await open();
    const signedOut = new Session(owner);
    const login = await signedOut.call("POST", "/auth/api/login", {
      data: { email: manifest.owner_email, password: manifest.password },
    });
    if (login.status !== 200) throw new Error(`oracle: owner sign-in returned HTTP ${login.status}`);
    const session = (await signedOut.call("GET", "/admin/api/session")).body;
    const csrf = session.actor.csrf_token as string;
    const admin = new Session(owner, { "x-azizam-admin-action": "1", "x-azizam-csrf": csrf });
    const member = new Session(owner, { "x-azizam-user-action": "1", "x-azizam-csrf": csrf });
    const plans = (await admin.call("GET", "/admin/api/profiles")).body.profiles as Json[];
    const plan = plans[0];

    // --- Accounts: an invite becomes a person, who signs in, is changed and removed.
    const email = "oracle-person@example.test";
    const password = "Oracle person password 2026";
    const invite = record(
      "POST /admin/api/invites",
      await admin.call("POST", "/admin/api/invites", { data: { profile_id: plan.id, email, expires_in: 3600 } }),
    );
    record("GET /admin/api/invites/{id}/link", await admin.call("GET", `/admin/api/invites/${invite.invite.id}/link`));
    const token = String(invite.url).split("token=")[1];
    const guest = new Session(await open());
    record("POST /auth/api/invite", await guest.call("POST", "/auth/api/invite", { data: { token } }));
    record(
      "POST /auth/api/invite/accept",
      await guest.call("POST", "/auth/api/invite/accept", { data: { token, email, name: "Oracle Person", password } }),
    );
    record(
      "POST /auth/api/login",
      await new Session(await open()).call("POST", "/auth/api/login", { data: { email, password } }),
    );
    const people = (await admin.call("GET", "/admin/api/people")).body.people as Json[];
    const person = people.find((row) => row.email === email);
    if (!person) throw new Error("oracle: the invited person is not listed");
    record("PATCH /admin/api/people/{id}", await admin.call("PATCH", `/admin/api/people/${person.id}`, { data: { disabled: false } }));
    const reset = record(
      "POST /admin/api/people/{id}/reset",
      await admin.call("POST", `/admin/api/people/${person.id}/reset`, { data: { expires_in: 3600 } }),
    );
    record(
      "POST /auth/api/password-reset",
      await new Session(await open()).call("POST", "/auth/api/password-reset", {
        data: { token: String(reset.url).split("reset=")[1], password: "Oracle person password 2027" },
      }),
    );
    record("DELETE /admin/api/people/{id}", await admin.call("DELETE", `/admin/api/people/${person.id}`));
    const spare = (
      await admin.call("POST", "/admin/api/invites", { data: { profile_id: plan.id, expires_in: 3600 } })
    ).body;
    record("DELETE /admin/api/invites/{id}", await admin.call("DELETE", `/admin/api/invites/${spare.invite.id}`));

    // --- Plans, services, policy.
    record("PATCH /admin/api/profiles/{id}", await admin.call("PATCH", `/admin/api/profiles/${plan.id}`, { data: { name: plan.name } }));
    const registry = await owner.get("/admin/api/operator/service-registry");
    const generation = registry.headers()["etag"];
    const serviceIds = Object.keys((await registry.json()).service_registry.services);
    record(
      "PATCH /admin/api/operator/services/{id}",
      await admin.call("PATCH", `/admin/api/operator/services/${serviceIds[0]}`, {
        data: { note: "Oracle note" },
        headers: { "if-match": generation },
      }),
    );
    record(
      "POST /admin/api/operator/services/test",
      await admin.call("POST", "/admin/api/operator/services/test", { data: { base_url: engineOrigin, health_endpoint: "/health" } }),
    );
    record(
      "POST /admin/api/policy/simulate",
      await admin.call("POST", "/admin/api/policy/simulate", {
        data: { profile: plan.id, request: { model: "fast", intent: "chat.completions" } },
      }),
    );

    // --- Category policy: the first candidate becomes the active snapshot.
    const categories = "/admin/api/categories";
    const candidate = record(
      "POST /admin/api/categories/candidates",
      await admin.call("POST", `${categories}/candidates`, {
        data: { base_revision: null, reason: "Oracle", snapshot: categorySnapshot() },
      }),
    );
    const activated = await admin.call("POST", `${categories}/candidates/${candidate.revision}/activate`, {
      data: { expected_active_revision: null },
    });
    if (activated.status !== 200) throw new Error(`oracle: activating the candidate returned HTTP ${activated.status}`);
    record("GET /admin/api/categories/active", await admin.call("GET", `${categories}/active`));

    // --- Engines the gateway only reaches through a registered service: a
    // transcription engine, and the video workflow that serve.py's extra config
    // names. Plans must allow both before an app key can use them.
    const sttRegistry = await owner.get("/admin/api/operator/service-registry");
    const added = await admin.call("POST", "/admin/api/operator/services", {
      data: {
        id: "stt",
        name: "Oracle transcription",
        base_url: engineOrigin,
        protocol: "openai-compatible",
        capabilities: ["audio", "transcriptions"],
        health_endpoint: "/health",
        models: [{ public: "whisper-large", upstream: "whisper-large" }],
        model_policy_schema_version: 1,
        enabled: true,
      },
      headers: { "if-match": sttRegistry.headers()["etag"] },
    });
    if (added.status !== 201) throw new Error(`oracle: adding the transcription service returned HTTP ${added.status}`);
    const decisionsRegistry = await owner.get("/admin/api/operator/service-registry");
    const decider = await admin.call("POST", "/admin/api/operator/services", {
      data: {
        id: "decisions",
        name: "Oracle decisions",
        base_url: engineOrigin,
        protocol: "openai-compatible",
        capabilities: ["decisions"],
        health_endpoint: "/health",
        models: [{ public: "decide", upstream: "oracle-decider-v1" }],
        model_policy_schema_version: 1,
        enabled: true,
      },
      headers: { "if-match": decisionsRegistry.headers()["etag"] },
    });
    if (decider.status !== 201) throw new Error(`oracle: adding the decisions service returned HTTP ${decider.status}`);
    const embedding = await owner.get("/admin/api/operator/service-registry");
    const enabled = await admin.call("PATCH", "/admin/api/operator/services/household-embedding", {
      data: { enabled: true },
      headers: { "if-match": embedding.headers()["etag"] },
    });
    if (enabled.status !== 200) throw new Error(`oracle: enabling the embedding service returned HTTP ${enabled.status}`);
    await admin.call("POST", "/admin/api/config/reload", { data: {} });
    for (const row of plans) {
      const profile = row.profile as Json;
      const changed = await admin.call("PATCH", `/admin/api/profiles/${row.id}`, {
        data: {
          profile: {
            ...profile,
            backends: [...new Set([...profile.backends, "stt", "decisions"])],
            protocols: [...new Set([...(profile.protocols ?? []), "typesafe"])],
            models: [...new Set([...profile.models, "whisper-large", "video-ltx-2.5-t2v", "decide"])],
          },
        },
      });
      if (changed.status !== 200) throw new Error(`oracle: widening plan ${row.name} returned HTTP ${changed.status}`);
    }

    // --- An app key, as the apps in the household seed use.
    const app = Object.values(manifest.apps as Record<string, string>)[0];
    const keyed = record(
      "POST /me/api/apps/{id}/keys",
      await member.call("POST", `/me/api/apps/${app}/keys`, { data: { label: "Oracle key" } }),
    );
    const apiKey = keyed.credential.api_key as string;
    record(
      "POST /me/api/connection-test",
      await member.call("POST", "/me/api/connection-test", { data: { api_key: apiKey } }),
    );
    const client = new Session(await open(), { authorization: `Bearer ${apiKey}` });

    // --- Generation, with the fake engine behind each route.
    record(
      "POST /openai/v1/chat/completions",
      await client.call("POST", "/openai/v1/chat/completions", {
        data: { model: "fast", messages: [{ role: "user", content: "Say hello." }] },
      }),
    );
    record("POST /openai/v1/embeddings", await client.call("POST", "/openai/v1/embeddings", { data: { model: "bge-m3", input: "hello" } }));
    record(
      "POST /v1/images/generations",
      await client.call("POST", "/v1/images/generations", { data: { model: "krea2-turbo", prompt: "A red kite" } }),
    );
    record(
      "POST /openai/v1/audio/transcriptions",
      await client.call("POST", "/openai/v1/audio/transcriptions", {
        multipart: {
          file: { name: "clip.wav", mimeType: "audio/wav", buffer: silentWav() },
          model: "whisper-large",
          // A field that makes the request ineligible for chunking, which would
          // start ffprobe; the fixture gateway forbids that subprocess.
          "timestamp_granularities[]": "segment",
        },
      }),
    );
    record(
      "POST /v1/systemone",
      await client.call("POST", "/v1/systemone", {
        data: {
          model: "decide",
          state: "It is raining and the bus is late.",
          questions: { route: { type: "choice", criteria: { "Take a taxi": "Take a taxi", "Wait for the bus": "Wait for the bus" } } },
        },
      }),
    );
    const video = record("POST /v1/videos", await client.call("POST", "/v1/videos", { data: { prompt: "A red kite", seconds: "4" } }));
    record(
      "GET /v1/videos/{id}",
      await settle(
        () => client.call("GET", `/v1/videos/${video.id}`),
        (result) => ["completed", "failed"].includes(result.body.status),
        `video ${video.id}`,
      ),
    );
    const job = record(
      "POST /gateway/v1/media/jobs/image-generations",
      await client.call("POST", "/gateway/v1/media/jobs/image-generations", { data: { model: "krea2-turbo", prompt: "A red lamp" } }),
    );
    // The job runs on the fake engine; its content exists once it has succeeded.
    const finished = await settle(
      () => client.call("GET", `/gateway/v1/media/jobs/${job.id}`),
      (result) => ["succeeded", "failed", "cancelled"].includes(result.body.status),
      `media job ${job.id}`,
    );
    record("GET /gateway/v1/media/jobs/{id}", finished);
    record("GET /gateway/v1/media/jobs/{id}/content", await client.call("GET", `/gateway/v1/media/jobs/${job.id}/content`));

    // --- The member's library and receipts.
    const library = (await member.call("GET", "/me/api/media?limit=5")).body.assets as Json[];
    const asset = library.find((row) => row.request_id) ?? library[0];
    record("GET /me/api/media/{id}", await member.call("GET", `/me/api/media/${asset.id}`));
    record("GET /me/api/media/{id}/download", await member.call("GET", `/me/api/media/${asset.id}/download`));
    record("GET /me/api/receipts/{id}", await member.call("GET", `/me/api/receipts/${asset.request_id}`));
    record("DELETE /me/api/media/{id}", await member.call("DELETE", `/me/api/media/${asset.id}`));
  } finally {
    await Promise.all(contexts.map((context) => context.dispose()));
  }
  return { bodies };
}

function identityBodies(python: string): Record<string, unknown> {
  const run = spawnSync(python, ["scripts/ui/identity_contract.py"], {
    cwd: path.resolve(WEB, ".."),
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  if (run.status !== 0) throw new Error(`oracle: identity_contract.py failed: ${run.stderr.slice(-400)}`);
  return JSON.parse(run.stdout.trim().split("\n").pop() ?? "{}");
}

// Starts the oracle gateway, exercises it, and stops it. `port` is the run's
// gateway port (UI_E2E_PORT); the oracle takes the block of ten ports below it.
export async function buildOracle(port: number, stateRoot: string, outFile: string): Promise<void> {
  const oraclePort = port - 10;
  const engineOrigin = `http://127.0.0.1:${oraclePort + 1}`;
  const root = path.join(stateRoot, "oracle");
  try {
    rmSync(root, { recursive: true });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  mkdirSync(root, { recursive: true });
  const extra = path.join(root, "extra-config.json");
  writeFileSync(
    extra,
    JSON.stringify({
      // ComfyUI is enabled in the registry but not in the config file: the media routes need both.
      backends: { comfyui: { enabled: true } },
      videos: {
        default_workflow: "video-ltx-2.5-t2v",
        workflows: {
          "video-ltx-2.5-t2v": {
            file: path.resolve(WEB, "../workflows/videos/video-ltx-2.5-t2v.json"),
            min_seconds: 1,
            max_seconds: 10,
            max_size: "1280x704",
            default_size: "960x512",
            size_multiple: 64,
            frame_rate: 24,
          },
        },
      },
      model_aliases: {
        decisions: {
          engines: {
            "oracle-decider": { display_name: "Oracle decisions", service: "decisions", model: "oracle-decider-v1", placement: "cpu", residency: "on_demand" },
          },
          stable: { decide: ["oracle-decider"] },
        },
        transcriptions: {
          engines: {
            "oracle-stt": { display_name: "Oracle transcription", service: "stt", model: "whisper-large", description: "Fixture engine" },
          },
          stable: { stt: ["oracle-stt"] },
          openai: [{ match: "whisper*", to: "stt" }],
        },
      },
    }),
  );
  const manifestPath = path.join(root, "manifest.json");
  const localPython = path.resolve(WEB, "../venv/bin/python");
  const python = process.env.UI_E2E_PYTHON ?? (existsSync(localPython) ? localPython : "python3");
  const log = path.join(root, "server.log");
  // Something else answering here would pass for the oracle: refuse to start.
  const occupied = await request.newContext();
  try {
    const taken = await occupied.get(`http://127.0.0.1:${oraclePort}/health`, { timeout: 2000 }).catch(() => undefined);
    if (taken) throw new Error(`oracle: port ${oraclePort} (the run's UI_E2E_PORT - 10) is already in use`);
  } finally {
    await occupied.dispose();
  }
  const child = spawn(python, ["../scripts/ui/serve.py"], {
    cwd: WEB,
    env: {
      ...process.env,
      UI_E2E_PORT: String(oraclePort),
      UI_E2E_ALLOWED_PORTS: "",
      UI_E2E_STATE_ROOT: root,
      UI_SEED_PROFILE: "household",
      UI_HOUSEHOLD_MANIFEST_PATH: manifestPath,
      UI_E2E_EXTRA_CONFIG: extra,
      UI_HOUSEHOLD_PROTOCOL_FRONTENDS: "builtin",
    },
    stdio: ["ignore", openSync(log, "w"), "inherit"],
  });
  const exited = new Promise<never>((_, reject) =>
    child.once("exit", (code) => reject(new Error(`oracle: the gateway exited with code ${code}; see ${log}`))),
  );
  try {
    const origin = `http://127.0.0.1:${oraclePort}`;
    const ready = (async () => {
      const probe = await request.newContext();
      try {
        for (;;) {
          const response = await probe.get(`${origin}/health`, { timeout: 2000 }).catch(() => undefined);
          if (response?.ok() && existsSync(manifestPath)) return;
          await new Promise((resolve) => setTimeout(resolve, 250));
        }
      } finally {
        await probe.dispose();
      }
    })();
    await Promise.race([ready, exited]);
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
    const file = await Promise.race([exerciseOracle(origin, manifest, engineOrigin), exited]);
    // The identity routes need a face service and a GPU workflow the fixture lacks:
    // their real handlers run over fakes in scripts/ui/identity_contract.py.
    Object.assign(file.bodies, identityBodies(python));
    writeFileSync(outFile, JSON.stringify(file));
  } catch (error) {
    // The gateway's own log is deleted with its state; keep its tail with the failure.
    for (const entry of readdirSync(root, { withFileTypes: true })) {
      const gatewayLog = path.join(root, entry.name, `${oraclePort}.log`);
      if (entry.isDirectory() && existsSync(gatewayLog))
        console.error(readFileSync(gatewayLog, "utf8").split("\n").slice(-400).join("\n"));
    }
    throw error;
  } finally {
    child.removeAllListeners("exit");
    child.kill("SIGTERM");
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        child.kill("SIGKILL");
        resolve();
      }, 10_000);
      child.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
      if (child.exitCode !== null) {
        clearTimeout(timer);
        resolve();
      }
    });
  }
}

// `node oracle.cjs <port> <state root> <out file>`: build the oracle file by hand.
if (require.main === module) {
  const [port, stateRoot, outFile] = process.argv.slice(2);
  buildOracle(Number(port), stateRoot, outFile).then(
    () => console.log(`oracle written to ${outFile}`),
    (error) => {
      console.error(error);
      process.exit(1);
    },
  );
}
