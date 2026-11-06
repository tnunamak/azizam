import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { PICTURE_ITEM, stubbedLibrary } from "../../../tests/helpers/library-stub";
import { MeshMode, type MeshModeProps } from "./mesh-panel";
import {
  MESH_BUSY_MESSAGE,
  MESH_CANCELED_MESSAGE,
  MESH_CANCELLING_MESSAGE,
  MESH_JOB_STORAGE_KEY,
  MESH_RECONNECTING_MESSAGE,
  MESH_STOPPED_MESSAGE,
  MESH_UNREACHABLE_MESSAGE,
  MESH_WAIT_MESSAGE,
} from "./mesh-mode";

vi.mock("@google/model-viewer", () => ({}));
// The wait between two polls: a few milliseconds here, so a test does not sit through the real 2 to 5 seconds.
const pollDelay = vi.hoisted(() => ({ ms: 5, unreachableMs: 120_000 }));
vi.mock("./mesh-mode", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./mesh-mode")>()),
  meshPollDelayMs: () => pollDelay.ms,
  meshUnreachableAfterMs: () => pollDelay.unreachableMs,
}));

const fetchMock = vi.fn();
const PICTURE = new File(["png"], "mug.png", { type: "image/png" });
const DETAIL: MeshModeProps["detail"] = {
  choices: [
    { value: 512, label: "512" },
    { value: 1024, label: "1024" },
    { value: 1536, label: "1536" },
  ],
  defaultValue: 1024,
};

function renderPanel(
  getSecret: MeshModeProps["getSecret"] = async () => "test-secret",
  options: { busy?: boolean; active?: boolean; initialFile?: File | null } = {},
) {
  const panel = (active: boolean) => (
    <MeshMode
      detail={DETAIL}
      getSecret={getSecret}
      busy={options.busy ?? false}
      active={active}
      initialFile={options.initialFile}
    />
  );
  const view = render(panel(options.active ?? true));
  return {
    ...view,
    setActive: (active: boolean) => view.rerender(panel(active)),
  };
}

async function choosePicture(container: HTMLElement) {
  const input = container.querySelector(
    'input[type="file"]',
  ) as HTMLInputElement;
  await userEvent.upload(input, PICTURE);
}

/** The request the panel sent: its URL, method, headers and multipart fields. */
function sent(call = 0) {
  const [url, init] = fetchMock.mock.calls[call] as [string, RequestInit];
  return { url, init, form: init.body as FormData };
}

const glb = () =>
  new Response(new Blob(["glTF"]), {
    status: 200,
    headers: { "content-type": "model/gltf-binary" },
  });
const refusal = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

beforeEach(() => {
  pollDelay.ms = 5;
  pollDelay.unreachableMs = 120_000;
  window.sessionStorage.clear();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal(
    "URL",
    Object.assign(URL, {
      createObjectURL: vi.fn(() => "blob:mesh-1"),
      revokeObjectURL: vi.fn(),
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

test("the picture field keeps its name: a group labelled Picture holds its controls", () => {
  renderPanel();
  const field = screen.getByRole("group", { name: "Picture" });
  expect(field.querySelector("#mesh-picture")).not.toBeNull();
  expect(screen.getByRole("button", { name: "Choose a file" })).toBeTruthy();
});

test("posts the picture and the chosen detail as multipart to the mesh route with the Try key", async () => {
  fetchMock.mockResolvedValue(glb());
  const { container } = renderPanel();
  await choosePicture(container);
  await userEvent.selectOptions(screen.getByLabelText(/^Detail/), "512");
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  const { url, init, form } = sent();
  expect(url).toBe("/v1/meshes/generate");
  expect(init.method).toBe("POST");
  expect(new Headers(init.headers).get("authorization")).toBe(
    "Bearer test-secret",
  );
  expect(form).toBeInstanceOf(FormData);
  expect((form.get("image") as File).name).toBe("mug.png");
  expect(form.get("resolution")).toBe("512");
  expect(form.has("seed")).toBe(false);
  expect(form.has("bg_removal")).toBe(false);
});

test("a picture chosen from the Library is the multipart file the mesh route gets", async () => {
  const library = stubbedLibrary([PICTURE_ITEM], (url) =>
    Promise.resolve(url === "/v1/meshes/generate" ? glb() : refusal(404, {})),
  );
  fetchMock.mockImplementation(library.impl);
  renderPanel();
  await userEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  await userEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
  expect(await screen.findByText("From your Library")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));

  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === "/v1/meshes/generate")).toBe(true));
  const call = fetchMock.mock.calls.find(([url]) => url === "/v1/meshes/generate") as [string, RequestInit];
  const image = (call[1].body as FormData).get("image") as File;
  expect(image.name).toBe("library-picture.png");
  expect(image.type).toBe("image/png");
  expect(library.listed[0]).toContain("kind=image");
});

test("a picture type the mesh route does not take is refused before it is sent", async () => {
  const { container } = renderPanel();
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [new File(["g"], "spin.gif", { type: "image/gif" })] } });
  expect(await screen.findByRole("alert")).toHaveTextContent("That file is not a PNG, JPEG or WebP picture.");
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));
  expect(await screen.findByText("Choose a picture first.")).toBeVisible();
  expect(fetchMock).not.toHaveBeenCalled();
});

test("sends the seed and background removal from the more-options fields", async () => {
  fetchMock.mockResolvedValue(glb());
  const { container } = renderPanel();
  await choosePicture(container);
  await userEvent.click(screen.getByText("More options"));
  await userEvent.type(screen.getByLabelText(/^Variation number/), "42");
  await userEvent.selectOptions(
    screen.getByLabelText(/^Background removal/),
    "threshold",
  );
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  expect(sent().form.get("seed")).toBe("42");
  expect(sent().form.get("bg_removal")).toBe("threshold");
});

test("shows the gateway's own message for a 503 and offers no viewer", async () => {
  fetchMock.mockResolvedValue(
    refusal(503, {
      error: {
        message: "The GPU is busy. Try again shortly.",
        code: "resource_unavailable",
      },
    }),
  );
  const { container } = renderPanel();
  await choosePicture(container);
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The GPU is busy. Try again shortly.",
  );
  expect(container.querySelector("model-viewer")).toBeNull();
  expect(screen.getByRole("button", { name: "Make 3D model" })).toBeEnabled();
});

test("falls back to plain copy when a failure carries no message", async () => {
  fetchMock.mockResolvedValue(
    new Response("<html>bad gateway</html>", { status: 502 }),
  );
  const { container } = renderPanel();
  await choosePicture(container);
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The 3D model could not be made. Try again.",
  );
});

test("renders the viewer for a 200 GLB, described by the picture's name", async () => {
  fetchMock.mockResolvedValue(glb());
  const { container } = renderPanel();
  await choosePicture(container);
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));

  await waitFor(() =>
    expect(container.querySelector("model-viewer")).not.toBeNull(),
  );
  expect(container.querySelector("model-viewer")?.getAttribute("alt")).toBe(
    "3D model made from the picture mug.png",
  );
  expect(screen.getByRole("link", { name: "Download GLB" })).toHaveAttribute(
    "download",
    expect.stringMatching(/^azizam-3d-.*\.glb$/),
  );
});

test("unmounting while the submit is open stops it", async () => {
  let signal: AbortSignal | undefined;
  fetchMock.mockImplementation((_url: string, init: RequestInit) => {
    signal = init.signal ?? undefined;
    return new Promise(() => {});
  });
  const { container, unmount } = renderPanel();
  await choosePicture(container);
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));
  await waitFor(() => expect(signal).toBeDefined());
  unmount();
  expect(signal?.aborted).toBe(true);
});

test("asks for a picture before anything is sent", async () => {
  renderPanel();
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Choose a picture first.",
  );
  expect(fetchMock).not.toHaveBeenCalled();
});

test("refuses a seed outside 0 to 4294967295 before anything is sent", async () => {
  const { container } = renderPanel();
  await choosePicture(container);
  await userEvent.click(screen.getByText("More options"));
  await userEvent.type(screen.getByLabelText(/^Variation number/), "-1");
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Enter a whole number from 0 to 4294967295",
  );
  expect(fetchMock).not.toHaveBeenCalled();
});

test("sends nothing when Try cannot prepare a key", async () => {
  const { container } = renderPanel(async () => null);
  await choosePicture(container);
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));
  await act(async () => {});
  expect(fetchMock).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Make 3D model" })).toBeEnabled();
});

test("while the GPU is busy, the form stays but cannot send, and says to try again shortly", async () => {
  const { container } = renderPanel(undefined, { busy: true });
  await choosePicture(container);
  expect(screen.getByRole("button", { name: "Make 3D model" })).toBeDisabled();
  expect(screen.getByRole("status")).toHaveTextContent(MESH_BUSY_MESSAGE);
  expect(fetchMock).not.toHaveBeenCalled();
});

test("the viewer mounts only while its tab is open, and a finished model is kept for when it is", async () => {
  fetchMock.mockResolvedValue(glb());
  const { container, setActive } = renderPanel(undefined, { active: false });
  await choosePicture(container);
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull(),
  );
  expect(container.querySelector("model-viewer")).toBeNull();

  setActive(true);
  await waitFor(() =>
    expect(container.querySelector("model-viewer")).not.toBeNull(),
  );
  setActive(false);
  expect(container.querySelector("model-viewer")).toBeNull();
});

test("a picture sent from Home starts the model by itself, once, with the default detail", async () => {
  fetchMock.mockResolvedValue(glb());
  renderPanel(undefined, { initialFile: PICTURE });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  const { form } = sent();
  expect((form.get("image") as File).name).toBe("mug.png");
  expect(form.get("resolution")).toBe("1024");
  await screen.findByRole("status");
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

test("a picture sent from Home waits while the GPU is busy and does not start", async () => {
  renderPanel(undefined, { initialFile: PICTURE, busy: true });
  await screen.findByText(MESH_BUSY_MESSAGE);
  expect(fetchMock).not.toHaveBeenCalled();
});

// --- the async form: a job the gateway runs, and the panel follows ----------------------------------------------

const JOB_ID = "mj_abc";
const job = (status: string, extra: Record<string, unknown> = {}) =>
  new Response(JSON.stringify({ object: "media.job", id: JOB_ID, status, ...extra }), {
    status: status === "queued" ? 202 : 200,
    headers: { "content-type": "application/json" },
  });

type Script = {
  /** What each poll answers, in order; the last one repeats. An Error is thrown as a dropped connection. */
  polls: Array<string | Error | Response | (() => Promise<Response>)>;
  cancel?: () => Response;
};

/** A gateway that answers the submit with a job, then the polls and the content as scripted. */
function serve(script: Script) {
  let poll = 0;
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/v1/meshes/generate") return job("queued");
    if (url.endsWith("/cancel")) return script.cancel?.() ?? job("running", { cancel_requested: true });
    if (url.endsWith("/content")) return glb();
    const next = script.polls[Math.min(poll, script.polls.length - 1)];
    poll += 1;
    if (next instanceof Error) throw next;
    if (typeof next === "function") return next();
    return next instanceof Response ? next.clone() : job(next);
  });
}
const urls = () => fetchMock.mock.calls.map(([url]) => url as string);
const polled = () => urls().filter((url) => url === `/gateway/v1/media/jobs/${JOB_ID}`);

async function start(container: HTMLElement) {
  await choosePicture(container);
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));
}

test("asks to be answered as a job, follows it, and shows the model when it succeeds", async () => {
  // The second poll answers only after the waiting status is checked, so a slow host cannot skip past it.
  let release = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  serve({ polls: ["running", () => held.then(() => job("running")), "succeeded"] });
  const { container } = renderPanel();
  await start(container);

  expect(await screen.findByRole("status")).toHaveTextContent(MESH_WAIT_MESSAGE);
  expect(screen.getByRole("status")).toHaveTextContent(/Elapsed \d+:\d\d/);
  release();
  await waitFor(() =>
    expect(container.querySelector("model-viewer")).not.toBeNull(),
  );
  expect(new Headers(sent().init.headers).get("prefer")).toBe("respond-async");
  expect(new Headers(sent().init.headers).get("authorization")).toBe("Bearer test-secret");
  expect(polled().length).toBe(3);
  expect(urls()).toContain(`/gateway/v1/media/jobs/${JOB_ID}/content`);
  expect(window.sessionStorage.getItem(MESH_JOB_STORAGE_KEY)).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("keeps the job id in sessionStorage while it runs", async () => {
  serve({ polls: ["running"] });
  const { container } = renderPanel();
  await start(container);
  await waitFor(() => expect(polled().length).toBeGreaterThan(0));
  const stored = JSON.parse(window.sessionStorage.getItem(MESH_JOB_STORAGE_KEY) ?? "null");
  expect(stored).toMatchObject({ id: JOB_ID, picture: "mug.png" });
  expect(typeof stored.startedAt).toBe("number");
});

test("a dropped connection while polling shows Reconnecting and is not an error", async () => {
  serve({
    polls: [
      "running",
      new TypeError("Failed to fetch"),
      new TypeError("Failed to fetch"),
      "running",
      "succeeded",
    ],
  });
  const { container } = renderPanel();
  await start(container);

  await screen.findByText(MESH_RECONNECTING_MESSAGE);
  expect(screen.queryByRole("alert")).toBeNull();
  await waitFor(() =>
    expect(container.querySelector("model-viewer")).not.toBeNull(),
  );
  expect(screen.queryByText(MESH_RECONNECTING_MESSAGE)).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a server fault while polling is a wait too", async () => {
  serve({ polls: [new Response("bad gateway", { status: 502 }), "succeeded"] });
  const { container } = renderPanel();
  await start(container);
  await waitFor(() =>
    expect(container.querySelector("model-viewer")).not.toBeNull(),
  );
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a server fault that never clears ends as 'Azizam is not answering', keeps the job id, and Check again finds the job", async () => {
  pollDelay.unreachableMs = 40; // a few polls of 5 ms stand for two minutes
  let down = true;
  fetchMock.mockImplementation(async (url: string) => {
    if (url === "/v1/meshes/generate") return job("queued");
    if (url.endsWith("/content")) return glb();
    return down ? new Response("bad gateway", { status: 502 }) : job("succeeded");
  });
  const { container } = renderPanel();
  await start(container);

  expect(await screen.findByRole("alert")).toHaveTextContent(MESH_UNREACHABLE_MESSAGE);
  expect(screen.queryByText(MESH_RECONNECTING_MESSAGE)).toBeNull();
  expect(screen.getByRole("button", { name: "Make 3D model" })).toBeDisabled();
  expect(JSON.parse(window.sessionStorage.getItem(MESH_JOB_STORAGE_KEY) ?? "null")).toMatchObject({ id: JOB_ID });
  // It stopped asking: no more polls while the person decides.
  const polls = polled().length;
  await new Promise((resolve) => setTimeout(resolve, 60));
  expect(polled().length).toBe(polls);

  down = false;
  await userEvent.click(screen.getByRole("button", { name: "Check again" }));
  await waitFor(() => expect(container.querySelector("model-viewer")).not.toBeNull());
  expect(urls().filter((url) => url === "/v1/meshes/generate").length).toBe(1); // the same job, not a new one
  expect(window.sessionStorage.getItem(MESH_JOB_STORAGE_KEY)).toBeNull();
});

test("a reload finds the job again: no new submit, the polling resumes and the model shows", async () => {
  window.sessionStorage.setItem(
    MESH_JOB_STORAGE_KEY,
    JSON.stringify({ id: JOB_ID, startedAt: Date.now() - 125_000, picture: "mug.png" }),
  );
  serve({ polls: ["running", "succeeded"] });
  const { container } = renderPanel();

  expect(await screen.findByRole("status")).toHaveTextContent(/Elapsed 2:0\d/);
  await waitFor(() =>
    expect(container.querySelector("model-viewer")).not.toBeNull(),
  );
  expect(urls()).not.toContain("/v1/meshes/generate");
  expect(container.querySelector("model-viewer")?.getAttribute("alt")).toBe(
    "3D model made from the picture mug.png",
  );
  expect(window.sessionStorage.getItem(MESH_JOB_STORAGE_KEY)).toBeNull();
});

test("a job the gateway no longer knows ends the resume with a plain message", async () => {
  window.sessionStorage.setItem(
    MESH_JOB_STORAGE_KEY,
    JSON.stringify({ id: JOB_ID, startedAt: Date.now(), picture: "mug.png" }),
  );
  serve({ polls: [new Response("{}", { status: 404 })] });
  renderPanel();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "That 3D model is no longer available. Make it again.",
  );
  expect(window.sessionStorage.getItem(MESH_JOB_STORAGE_KEY)).toBeNull();
});

test("coming back to the page asks at once instead of waiting out the delay", async () => {
  pollDelay.ms = 600_000;
  serve({ polls: ["running", "succeeded"] });
  const { container } = renderPanel();
  await start(container);
  await waitFor(() => expect(polled().length).toBe(1));

  document.dispatchEvent(new Event("visibilitychange"));
  await waitFor(() =>
    expect(container.querySelector("model-viewer")).not.toBeNull(),
  );
  expect(polled().length).toBe(2);
});

test("a failed job shows the job's own message and clears the stored id", async () => {
  serve({ polls: ["running", job("failed", { error: "The mesh service did not answer in time." })] });
  const { container } = renderPanel();
  await start(container);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The mesh service did not answer in time.",
  );
  expect(container.querySelector("model-viewer")).toBeNull();
  expect(window.sessionStorage.getItem(MESH_JOB_STORAGE_KEY)).toBeNull();
  expect(screen.getByRole("button", { name: "Make 3D model" })).toBeEnabled();
});

test("Stop waiting stops the polling and does not touch the job", async () => {
  serve({ polls: ["running"] });
  const { container } = renderPanel();
  await start(container);
  await waitFor(() => expect(polled().length).toBeGreaterThan(1));

  await userEvent.click(screen.getByRole("button", { name: "Stop waiting" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(MESH_STOPPED_MESSAGE);
  expect(screen.queryByRole("status")).toBeNull();
  const after = fetchMock.mock.calls.length;
  await new Promise((resolve) => setTimeout(resolve, 60));
  expect(fetchMock.mock.calls.length).toBe(after);
  expect(urls().some((url) => url.endsWith("/cancel"))).toBe(false);
  expect(window.sessionStorage.getItem(MESH_JOB_STORAGE_KEY)).toBeNull();
});

test("Cancel asks the gateway to cancel the job and follows it until it has ended", async () => {
  let canceled = false;
  serve({
    polls: ["running"],
    cancel: () => {
      canceled = true;
      return job("running", { cancel_requested: true });
    },
  });
  fetchMock.mockImplementation(
    ((inner) => async (url: string, init: RequestInit) => {
      if (canceled && url === `/gateway/v1/media/jobs/${JOB_ID}`) {
        // The engine has returned: the job ends as canceled.
        return job("canceled", { error: "Canceled by requester" });
      }
      return inner(url, init);
    })(fetchMock.getMockImplementation() as (url: string, init: RequestInit) => Promise<Response>),
  );
  const { container } = renderPanel();
  await start(container);
  await waitFor(() => expect(polled().length).toBeGreaterThan(0));

  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  const call = fetchMock.mock.calls.find(([url]) => (url as string).endsWith("/cancel"));
  expect(call).toBeDefined();
  expect(call?.[0]).toBe(`/gateway/v1/media/jobs/${JOB_ID}/cancel`);
  expect((call?.[1] as RequestInit).method).toBe("POST");
  await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  expect(await screen.findByRole("alert")).toHaveTextContent(MESH_CANCELED_MESSAGE);
  expect(container.querySelector("model-viewer")).toBeNull();
  expect(window.sessionStorage.getItem(MESH_JOB_STORAGE_KEY)).toBeNull();
});

test("while a cancel waits for the engine, the panel says so", async () => {
  serve({ polls: ["running"] });
  const { container } = renderPanel();
  await start(container);
  await waitFor(() => expect(polled().length).toBeGreaterThan(0));
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(await screen.findByText(MESH_CANCELLING_MESSAGE)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
});

test("a refusal at submit is still shown as it was, with no job stored", async () => {
  fetchMock.mockResolvedValue(
    refusal(503, { error: { message: "The GPU is busy. Try again shortly.", code: "resource_unavailable" } }),
  );
  const { container } = renderPanel();
  await start(container);
  expect(await screen.findByRole("alert")).toHaveTextContent("The GPU is busy. Try again shortly.");
  expect(window.sessionStorage.getItem(MESH_JOB_STORAGE_KEY)).toBeNull();
});
