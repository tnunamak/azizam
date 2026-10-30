import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { MeshMode, type MeshModeProps } from "./mesh-panel";
import { MESH_WAIT_MESSAGE } from "./mesh-mode";

vi.mock("@google/model-viewer", () => ({}));

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

function renderPanel(getSecret: MeshModeProps["getSecret"] = async () => "test-secret") {
  return render(<MeshMode detail={DETAIL} getSecret={getSecret} />);
}

async function choosePicture(container: HTMLElement) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  await userEvent.upload(input, PICTURE);
}

/** The request the panel sent: its URL, method, headers and multipart fields. */
function sent(call = 0) {
  const [url, init] = fetchMock.mock.calls[call] as [string, RequestInit];
  return { url, init, form: init.body as FormData };
}

const glb = () => new Response(new Blob(["glTF"]), { status: 200, headers: { "content-type": "model/gltf-binary" } });
const refusal = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:mesh-1"), revokeObjectURL: vi.fn() }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
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
  expect(new Headers(init.headers).get("authorization")).toBe("Bearer test-secret");
  expect(form).toBeInstanceOf(FormData);
  expect((form.get("image") as File).name).toBe("mug.png");
  expect(form.get("resolution")).toBe("512");
  expect(form.has("seed")).toBe(false);
  expect(form.has("bg_removal")).toBe(false);
});

test("sends the seed and background removal from the more-options fields", async () => {
  fetchMock.mockResolvedValue(glb());
  const { container } = renderPanel();
  await choosePicture(container);
  await userEvent.click(screen.getByText("More options"));
  await userEvent.type(screen.getByLabelText(/^Variation number/), "42");
  await userEvent.selectOptions(screen.getByLabelText(/^Background removal/), "threshold");
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  expect(sent().form.get("seed")).toBe("42");
  expect(sent().form.get("bg_removal")).toBe("threshold");
});

test("shows the gateway's own message for a 503 and offers no viewer", async () => {
  fetchMock.mockResolvedValue(
    refusal(503, { error: { message: "The GPU is busy. Try again shortly.", code: "resource_unavailable" } }),
  );
  const { container } = renderPanel();
  await choosePicture(container);
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("The GPU is busy. Try again shortly.");
  expect(container.querySelector("model-viewer")).toBeNull();
  expect(screen.getByRole("button", { name: "Make 3D model" })).toBeEnabled();
});

test("falls back to plain copy when a failure carries no message", async () => {
  fetchMock.mockResolvedValue(new Response("<html>bad gateway</html>", { status: 502 }));
  const { container } = renderPanel();
  await choosePicture(container);
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("The 3D model could not be made. Try again.");
});

test("renders the viewer for a 200 GLB, described by the picture's name", async () => {
  fetchMock.mockResolvedValue(glb());
  const { container } = renderPanel();
  await choosePicture(container);
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));

  await waitFor(() => expect(container.querySelector("model-viewer")).not.toBeNull());
  expect(container.querySelector("model-viewer")?.getAttribute("alt")).toBe("3D model made from the picture mug.png");
  expect(screen.getByRole("link", { name: "Download GLB" })).toHaveAttribute(
    "download",
    expect.stringMatching(/^azizam-3d-.*\.glb$/),
  );
});

test("says it can take about a minute, and Cancel stops the request", async () => {
  let aborted = false;
  fetchMock.mockImplementation(
    (_url: string, init: RequestInit) =>
      new Promise((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => {
          aborted = true;
          reject(new DOMException("The operation was aborted.", "AbortError"));
        });
      }),
  );
  const { container } = renderPanel();
  await choosePicture(container);
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));

  expect(screen.getByRole("status")).toHaveTextContent(MESH_WAIT_MESSAGE);
  expect(screen.getByRole("button", { name: "Make 3D model" })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

  expect(aborted).toBe(true);
  expect(await screen.findByRole("alert")).toHaveTextContent("You stopped waiting. The server may still finish the model.");
  expect(screen.queryByRole("status")).toBeNull();
});

test("unmounting while a request runs cancels it", async () => {
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
  expect(screen.getByRole("alert")).toHaveTextContent("Choose a picture first.");
  expect(fetchMock).not.toHaveBeenCalled();
});

test("refuses a seed outside 0 to 4294967295 before anything is sent", async () => {
  const { container } = renderPanel();
  await choosePicture(container);
  await userEvent.click(screen.getByText("More options"));
  await userEvent.type(screen.getByLabelText(/^Variation number/), "-1");
  await userEvent.click(screen.getByRole("button", { name: "Make 3D model" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Enter a whole number from 0 to 4294967295");
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
