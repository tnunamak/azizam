import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { MeshViewer } from "./mesh-viewer";

const mocks = vi.hoisted(() => ({ loaded: vi.fn() }));
// model-viewer needs WebGL, which jsdom lacks. The factory counts imports so the lazy-load test can see them.
const modelViewerMock = () => {
  mocks.loaded();
  return {};
};

const GLB = new Blob(["glTF"], { type: "model/gltf-binary" });
const NAME = "azizam-3d-2026-10-30T15-04-05.glb";
let urls: string[];
let createObjectURL: ReturnType<typeof vi.fn>;
let revokeObjectURL: ReturnType<typeof vi.fn>;

function viewerElement(container: HTMLElement) {
  return container.querySelector("model-viewer") as HTMLElement & {
    resetTurntableRotation: () => void;
    jumpCameraToGoal: () => void;
  };
}

/** Waits for the lazy import to put model-viewer on the page. */
async function mounted(container: HTMLElement) {
  await waitFor(() => expect(viewerElement(container)).not.toBeNull());
  return viewerElement(container);
}

beforeEach(() => {
  // A fresh module registry and mock per test: the factory runs on each test's first import, whatever the order.
  vi.resetModules();
  vi.doMock("@google/model-viewer", modelViewerMock);
  urls = [];
  createObjectURL = vi.fn(() => {
    const url = `blob:mesh-${urls.length + 1}`;
    urls.push(url);
    return url;
  });
  revokeObjectURL = vi.fn();
  vi.stubGlobal(
    "URL",
    Object.assign(URL, { createObjectURL, revokeObjectURL }),
  );
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: false,
      addEventListener() {},
      removeEventListener() {},
    })),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

test("does not load model-viewer until a model is shown, then loads it after mount", async () => {
  expect(mocks.loaded).not.toHaveBeenCalled();
  const { container } = render(
    <MeshViewer
      blob={GLB}
      downloadName={NAME}
      description="3D model made from mug.png"
    />,
  );
  const viewer = await mounted(container);
  expect(mocks.loaded).toHaveBeenCalledTimes(1);
  expect(viewer.getAttribute("alt")).toBe("3D model made from mug.png");
  expect(viewer.getAttribute("src")).toBe(urls[0]);
  expect(viewer.hasAttribute("camera-controls")).toBe(true);
});

test("auto-rotates unless the visitor asks for reduced motion", async () => {
  const { container, unmount } = render(
    <MeshViewer blob={GLB} downloadName={NAME} description="mug" />,
  );
  expect((await mounted(container)).hasAttribute("auto-rotate")).toBe(true);
  unmount();

  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: query === "(prefers-reduced-motion: reduce)",
      addEventListener() {},
      removeEventListener() {},
    })),
  );
  const again = render(
    <MeshViewer blob={GLB} downloadName={NAME} description="mug" />,
  );
  expect((await mounted(again.container)).hasAttribute("auto-rotate")).toBe(
    false,
  );
});

test("announces progress while loading and loaded when ready", async () => {
  const { container } = render(
    <MeshViewer blob={GLB} downloadName={NAME} description="mug" />,
  );
  const viewer = await mounted(container);
  act(() => {
    viewer.dispatchEvent(
      new CustomEvent("progress", { detail: { totalProgress: 0.42 } }),
    );
  });
  expect(screen.getByRole("status")).toHaveTextContent(
    "Loading the 3D model. 42%",
  );
  act(() => {
    viewer.dispatchEvent(new CustomEvent("load"));
  });
  expect(screen.getByRole("status")).toHaveTextContent("3D model loaded.");
  expect(screen.queryByRole("progressbar")).toBeNull();
});

test("a final 100% progress event after load keeps the model shown as loaded", async () => {
  const { container } = render(
    <MeshViewer blob={GLB} downloadName={NAME} description="mug" />,
  );
  const viewer = await mounted(container);
  act(() => {
    viewer.dispatchEvent(new CustomEvent("load"));
    viewer.dispatchEvent(
      new CustomEvent("progress", { detail: { totalProgress: 1 } }),
    );
  });
  expect(screen.getByRole("status")).toHaveTextContent("3D model loaded.");
});

test("shows an error state, not a blank canvas, when the model fails to load", async () => {
  const { container } = render(
    <MeshViewer blob={GLB} downloadName={NAME} description="mug" />,
  );
  const viewer = await mounted(container);
  act(() => {
    viewer.dispatchEvent(
      new CustomEvent("error", { detail: { type: "loadfailure" } }),
    );
  });
  expect(screen.getByRole("status")).toHaveTextContent(
    "could not be shown in this browser",
  );
  expect(
    screen.getByText("The 3D model could not be shown in this browser.", {
      selector: "div",
    }),
  ).toBeVisible();
  // The file is still there to download.
  expect(screen.getByRole("link", { name: "Download GLB" })).toHaveAttribute(
    "href",
    urls[0],
  );
});

test("a model that stalls without an error event gives up, and progress keeps it waiting", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const { container } = render(
      <MeshViewer blob={GLB} downloadName={NAME} description="mug" />,
    );
    const viewer = await mounted(container);
    act(() => {
      viewer.dispatchEvent(
        new CustomEvent("progress", { detail: { totalProgress: 0.5 } }),
      );
    });
    act(() => vi.advanceTimersByTime(25_000));
    act(() => {
      viewer.dispatchEvent(
        new CustomEvent("progress", { detail: { totalProgress: 1 } }),
      );
    });
    act(() => vi.advanceTimersByTime(25_000));
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading the 3D model.",
    );
    act(() => vi.advanceTimersByTime(30_000));
    expect(screen.getByRole("status")).toHaveTextContent(
      "could not be shown in this browser",
    );
  } finally {
    vi.useRealTimers();
  }
});

test("the download link carries the blob's object URL and the timestamped file name", async () => {
  const { container } = render(
    <MeshViewer blob={GLB} downloadName={NAME} description="mug" />,
  );
  await mounted(container);
  const link = screen.getByRole("link", { name: "Download GLB" });
  expect(link).toHaveAttribute("href", urls[0]);
  expect(link).toHaveAttribute("download", NAME);
  expect(createObjectURL).toHaveBeenCalledWith(GLB);
});

test("Reset view turns the camera back to its start once the model is ready", async () => {
  const { container } = render(
    <MeshViewer blob={GLB} downloadName={NAME} description="mug" />,
  );
  const viewer = await mounted(container);
  const reset = vi.fn();
  const jump = vi.fn();
  viewer.resetTurntableRotation = reset;
  viewer.jumpCameraToGoal = jump;
  const button = screen.getByRole("button", { name: "Reset view" });
  expect(button).toBeDisabled();
  act(() => {
    viewer.dispatchEvent(new CustomEvent("load"));
  });
  fireEvent.click(button);
  expect(reset).toHaveBeenCalledTimes(1);
  expect(jump).toHaveBeenCalledTimes(1);
});

test("replacing the blob revokes the old URL and shows the new model", async () => {
  const { container, rerender } = render(
    <MeshViewer blob={GLB} downloadName={NAME} description="mug" />,
  );
  await mounted(container);
  const second = new Blob(["glTF2"], { type: "model/gltf-binary" });
  rerender(<MeshViewer blob={second} downloadName={NAME} description="mug" />);
  await waitFor(() =>
    expect(viewerElement(container).getAttribute("src")).toBe(urls[1]),
  );
  expect(revokeObjectURL).toHaveBeenCalledWith(urls[0]);
  expect(revokeObjectURL).not.toHaveBeenCalledWith(urls[1]);
});

test("unmounting revokes the URL and removes the viewer so its GPU context is released", async () => {
  const { container, unmount } = render(
    <MeshViewer blob={GLB} downloadName={NAME} description="mug" />,
  );
  await mounted(container);
  const removed = vi.spyOn(viewerElement(container), "remove");
  unmount();
  expect(removed).toHaveBeenCalledTimes(1);
  expect(revokeObjectURL).toHaveBeenCalledWith(urls[0]);
  expect(container.querySelector("model-viewer")).toBeNull();
});

test("repeated open and close leaves no viewer or URL behind", async () => {
  for (let index = 0; index < 3; index += 1) {
    const { container, unmount } = render(
      <MeshViewer blob={GLB} downloadName={NAME} description="mug" />,
    );
    await mounted(container);
    unmount();
  }
  expect(document.querySelectorAll("model-viewer")).toHaveLength(0);
  expect(revokeObjectURL).toHaveBeenCalledTimes(3);
});
