import { describe, expect, test } from "vitest";
import {
  meshDetailChoices,
  meshDownloadName,
  meshFailureMessage,
  meshElapsed,
  meshForm,
  meshJobFrom,
  meshPollDelayMs,
  meshSeedProblem,
  MESH_JOB_STORAGE_KEY,
  MESH_WAIT_MESSAGE,
  readStoredMeshJob,
  writeStoredMeshJob,
} from "./mesh-mode";
import type { WorkflowInput } from "./workflow-inputs";

const manifestInputs: WorkflowInput[] = [
  {
    name: "image",
    type: "string",
    required: true,
    asset: true,
    media_type: "image/*",
    label: "picture",
  },
  { name: "seed", type: "integer", minimum: 0, maximum: 4294967295 },
  {
    name: "resolution",
    type: "integer",
    enum: [512, 1024, 1536],
    default: 1024,
    label: "detail",
  },
  { name: "bg_removal", type: "string", enum: ["threshold", "birefnet"] },
];

describe("meshDetailChoices", () => {
  test("lists the manifest's resolution values and selects its default", () => {
    expect(meshDetailChoices(manifestInputs)).toEqual({
      choices: [
        { value: 512, label: "512" },
        { value: 1024, label: "1024" },
        { value: 1536, label: "1536" },
      ],
      defaultValue: 1024,
    });
  });

  test("selects the first value when the manifest names no default", () => {
    const inputs = manifestInputs.map((input) =>
      input.name === "resolution" ? { ...input, default: undefined } : input,
    );
    expect(meshDetailChoices(inputs).defaultValue).toBe(512);
  });

  test("offers nothing when the manifest has no resolution input", () => {
    expect(meshDetailChoices([])).toEqual({ choices: [], defaultValue: null });
  });
});

describe("meshSeedProblem", () => {
  test.each(["", "   "])(
    "an empty seed (%j) lets the service choose",
    (seed) => {
      expect(meshSeedProblem(seed)).toBeNull();
    },
  );

  test.each(["0", "42", " 4294967295 "])("accepts %j", (seed) => {
    expect(meshSeedProblem(seed)).toBeNull();
  });

  test.each(["-1", "1.5", "4294967296", "abc", "1e3"])("refuses %j", (seed) => {
    expect(meshSeedProblem(seed)).toMatch(/whole number from 0 to 4294967295/);
  });
});

describe("meshForm", () => {
  const image = new File(["x"], "mug.png", { type: "image/png" });

  test("sends the picture and detail, and leaves out a blank seed and background", () => {
    const form = meshForm({
      image,
      seed: "",
      resolution: 1536,
      background: "",
    });
    expect([...form.keys()]).toEqual(["image", "resolution"]);
    expect(form.get("resolution")).toBe("1536");
    expect((form.get("image") as File).name).toBe("mug.png");
  });

  test("sends the seed and background when they are chosen", () => {
    const form = meshForm({
      image,
      seed: " 7 ",
      resolution: 512,
      background: "birefnet",
    });
    expect(form.get("seed")).toBe("7");
    expect(form.get("bg_removal")).toBe("birefnet");
    expect(form.has("extensions")).toBe(false);
  });
});

describe("meshDownloadName", () => {
  test("names the file with a sortable UTC timestamp and no colons", () => {
    expect(meshDownloadName(new Date("2026-10-30T15:04:05.678Z"))).toBe(
      "azizam-3d-2026-10-30T15-04-05.glb",
    );
  });
});

describe("meshFailureMessage", () => {
  test("shows the server's own message", () => {
    expect(
      meshFailureMessage({
        error: {
          message: "The GPU is busy. Try again shortly.",
          code: "resource_unavailable",
        },
      }),
    ).toBe("The GPU is busy. Try again shortly.");
  });

  test.each([null, "<html>", { error: {} }, { error: { message: "  " } }])(
    "falls back when the reply has no message (%j)",
    (payload) => {
      expect(meshFailureMessage(payload)).toBe(
        "The 3D model could not be made. Try again.",
      );
    },
  );
});

describe("the async job helpers", () => {
  test("the poll delay starts at 2 s and backs off to 5 s", () => {
    expect([0, 1, 2, 5, 6, 50].map(meshPollDelayMs)).toEqual([2000, 2500, 3000, 4500, 5000, 5000]);
    expect(meshPollDelayMs(-3)).toBe(2000);
  });

  test("elapsed time reads as m:ss", () => {
    expect(meshElapsed(0)).toBe("0:00");
    expect(meshElapsed(9_999)).toBe("0:09");
    expect(meshElapsed(125_000)).toBe("2:05");
    expect(meshElapsed(-5)).toBe("0:00");
  });

  test("the wait copy is honest: several minutes, and the Library", () => {
    expect(MESH_WAIT_MESSAGE).toMatch(/several minutes/);
    expect(MESH_WAIT_MESSAGE).toMatch(/Library/);
    expect(MESH_WAIT_MESSAGE).not.toMatch(/about a minute/);
  });

  test("a job is read from the submit answer, or is null", () => {
    expect(meshJobFrom({ id: "mj_1", status: "running" })).toEqual({ id: "mj_1", status: "running" });
    expect(meshJobFrom({ job_id: "mj_2" })).toEqual({ id: "mj_2", status: "queued" });
    expect(meshJobFrom({ id: "" })).toBeNull();
    expect(meshJobFrom({ error: "x" })).toBeNull();
    expect(meshJobFrom(null)).toBeNull();
    expect(meshJobFrom("mj_1")).toBeNull();
  });

  test("a stored job round-trips, and a bad record reads as none", () => {
    window.sessionStorage.clear();
    expect(readStoredMeshJob(window.sessionStorage)).toBeNull();
    writeStoredMeshJob(window.sessionStorage, { id: "mj_1", startedAt: 5, picture: "a.png" });
    expect(readStoredMeshJob(window.sessionStorage)).toEqual({ id: "mj_1", startedAt: 5, picture: "a.png" });
    writeStoredMeshJob(window.sessionStorage, null);
    expect(window.sessionStorage.getItem(MESH_JOB_STORAGE_KEY)).toBeNull();
    window.sessionStorage.setItem(MESH_JOB_STORAGE_KEY, "{not json");
    expect(readStoredMeshJob(window.sessionStorage)).toBeNull();
    window.sessionStorage.setItem(MESH_JOB_STORAGE_KEY, JSON.stringify({ id: "mj_1" }));
    expect(readStoredMeshJob(window.sessionStorage)).toBeNull();
    window.sessionStorage.clear();
  });
});
