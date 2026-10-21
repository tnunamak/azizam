// "Choose from Library" on a media input. The Library's download route answers
// with a short-lived signed link, not the picture, so a pick must read the file
// from that link and the request must carry the picture's own bytes.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { MediaInput } from "./workflow-media-input";

const ID = `img_${"a".repeat(64)}`;
const SIGNED = "http://gateway.test/v1/assets/signed-picture";
const THUMB = "http://gateway.test/v1/assets/signed-thumbnail";
const PNG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 4];

const reply = (body: string, type: string, ok = true) =>
  ({
    ok,
    status: ok ? 200 : 502,
    headers: new Headers({ "content-type": type }),
    json: async () => JSON.parse(body),
  }) as unknown as Response;

/** A file answer whose body is streamed in `chunks`, counting the pulls. */
function fileAnswer(
  chunks: Uint8Array[],
  type: string,
  extra: Record<string, string> = {},
) {
  const stats = { pulls: 0, cancelled: false };
  let next = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      stats.pulls += 1;
      if (next < chunks.length) controller.enqueue(chunks[next++]);
      else controller.close();
    },
    cancel() {
      stats.cancelled = true;
    },
  });
  const response = new Response(body, {
    status: 200,
    headers: { "content-type": type, ...extra },
  });
  return { response, stats };
}

const library = {
  assets: [
    {
      asset_id: ID,
      kind: "image",
      media_type: "image/png",
      prompt: "A teal fish",
      available: true,
      thumbnail_url: THUMB,
      download_url: SIGNED,
    },
  ],
};

type Served = { file: Uint8Array[] | string; type?: string; headers?: Record<string, string>; listed?: object };

/** The gateway as the picker sees it. `hold` keeps the file answer back until released. */
function route(served: Served = { file: [new Uint8Array(PNG)] }, hold = false) {
  const signals: AbortSignal[] = [];
  const pending: Array<() => void> = [];
  const stats: Array<{ pulls: number; cancelled: boolean }> = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/me/api/media?"))
      return reply(
        JSON.stringify({ assets: [{ ...library.assets[0], ...served.listed }] }),
        "application/json",
      );
    if (url.startsWith(`/me/api/media/${ID}/download`)) {
      if (init?.signal) signals.push(init.signal);
      return reply(JSON.stringify({ asset_id: ID, download_url: SIGNED }), "application/json");
    }
    if (url === SIGNED) {
      if (init?.signal) signals.push(init.signal);
      if (hold) {
        await new Promise<void>((resolve, reject) => {
          pending.push(resolve);
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        });
      }
      if (typeof served.file === "string") return reply(served.file, "text/html");
      const answer = fileAnswer(served.file, served.type ?? "image/png", served.headers);
      stats.push(answer.stats);
      return answer.response;
    }
    return reply("{}", "application/json", false);
  });
  return { mock, signals, release: () => pending.splice(0).forEach((resolve) => resolve()), stats };
}

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
  URL.createObjectURL = vi.fn(() => "blob:chosen");
  URL.revokeObjectURL = vi.fn();
});
afterEach(() => {
  cleanup();
});

function FileHarness({ onFile }: { onFile: (file: File | null) => void }) {
  const [file, setFile] = useState<File | null>(null);
  return (
    <MediaInput
      id="picture"
      label="Picture to change"
      kind="image"
      file={file}
      onFileChange={(next) => {
        setFile(next);
        onFile(next);
      }}
    />
  );
}

function ReferenceHarness({ onValue }: { onValue: (value: string | undefined) => void }) {
  const [value, setValue] = useState<string | undefined>();
  return (
    <MediaInput
      id="picture"
      label="Starting picture"
      kind="image"
      value={value}
      onChange={(next) => {
        setValue(next);
        onValue(next);
      }}
    />
  );
}

async function openAndPick() {
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
}

test("the Library tiles show the list's signed thumbnail, not the download route", async () => {
  vi.stubGlobal("fetch", route().mock);
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  const tile = await screen.findByRole("button", { name: "A teal fish" });
  expect(tile.querySelector("img")?.getAttribute("src")).toBe(THUMB);
});

test("a file input gets the picture's bytes and type, not the download link's JSON", async () => {
  const fetchMock = route().mock;
  vi.stubGlobal("fetch", fetchMock);
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await openAndPick();
  await waitFor(() => expect(onFile).toHaveBeenCalledTimes(1));
  const file = onFile.mock.calls[0][0] as File;
  expect(file.type).toBe("image/png");
  expect(file.name).toBe("library-picture.png");
  const read = await new Promise<ArrayBuffer>((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.readAsArrayBuffer(file);
  });
  expect(Array.from(new Uint8Array(read))).toEqual(PNG);
  expect(fetchMock).toHaveBeenCalledWith(SIGNED, expect.anything());
  expect(await screen.findByText("From your Library")).toBeTruthy();
});

test("a reference input gets the picture as a data URI, never a bare asset id", async () => {
  vi.stubGlobal("fetch", route().mock);
  const onValue = vi.fn();
  render(<ReferenceHarness onValue={onValue} />);
  await openAndPick();
  await waitFor(() => expect(onValue).toHaveBeenCalledTimes(1));
  const value = onValue.mock.calls[0][0] as string;
  expect(value).toMatch(/^data:image\/png;base64,/);
  expect(value).not.toContain(ID);
});

test("an error page from the link is not added as a picture", async () => {
  vi.stubGlobal("fetch", route({ file: "<html>expired</html>" }).mock);
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await openAndPick();
  expect((await screen.findByRole("alert")).textContent).toContain("Could not add that picture from your Library");
  expect(onFile).not.toHaveBeenCalled();
});

const MB = 1024 * 1024;
const megabyte = new Uint8Array([...PNG, ...new Array(MB - PNG.length).fill(7)]);
const megabyteChunks = (count: number) => Array.from({ length: count }, () => megabyte);

async function waitForTooBig() {
  expect((await screen.findByRole("alert")).textContent).toContain("Choose one under 10 MB");
}

test("a picture the list says is over the limit is refused before any download", async () => {
  const served = route({ file: [new Uint8Array(PNG)], listed: { size_bytes: 11 * MB } });
  vi.stubGlobal("fetch", served.mock);
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await openAndPick();
  await waitForTooBig();
  expect((await screen.findByRole("alert")).textContent).toContain("11 MB");
  const urls = served.mock.mock.calls.map(([url]) => String(url));
  expect(urls.filter((url) => url === SIGNED || url.includes("/download"))).toEqual([]);
  expect(onFile).not.toHaveBeenCalled();
});

test("a Content-Length over the limit is refused without reading the body", async () => {
  const served = route({ file: megabyteChunks(30), headers: { "content-length": String(30 * MB) } });
  vi.stubGlobal("fetch", served.mock);
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await openAndPick();
  await waitForTooBig();
  expect(served.stats[0].pulls).toBeLessThanOrEqual(2);
  expect(served.stats[0].cancelled).toBe(true);
  expect(onFile).not.toHaveBeenCalled();
});

test("a body that passes the limit with no or a wrong Content-Length stops at the limit", async () => {
  const served = route({ file: megabyteChunks(40), headers: { "content-length": "1000" } });
  vi.stubGlobal("fetch", served.mock);
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await openAndPick();
  await waitForTooBig();
  expect(served.stats[0].pulls).toBeLessThan(15);
  expect(served.stats[0].cancelled).toBe(true);
  expect(onFile).not.toHaveBeenCalled();
});

test.each([
  ["a sound served as a picture", [0x49, 0x44, 0x33, 3, 0, 0, 0, 0, 0, 0x21, 0, 0]],
  ["a page served as a picture", [...new TextEncoder().encode("<!doctype html><html>")]],
  ["a video served as a picture", [0, 0, 0, 0x18, 0x66, 0x74, 0x79, 0x70, 0x69, 0x73, 0x6f, 0x6d]],
])("%s is not added", async (_name, bytes) => {
  vi.stubGlobal("fetch", route({ file: [new Uint8Array(bytes)] }).mock);
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await openAndPick();
  expect((await screen.findByRole("alert")).textContent).toContain("Could not add that picture from your Library");
  expect(onFile).not.toHaveBeenCalled();
});

test("a file with no declared type is added when the gateway's type and the bytes agree", async () => {
  vi.stubGlobal("fetch", route({ file: [new Uint8Array(PNG)], type: "application/octet-stream" }).mock);
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await openAndPick();
  await waitFor(() => expect(onFile).toHaveBeenCalledTimes(1));
  expect((onFile.mock.calls[0][0] as File).type).toBe("image/png");
});

// A pick still arriving must not outlive the action that replaced it.
function InForm({ onSubmit, children }: { onSubmit: () => void; children: React.ReactNode }) {
  return (
    <form
      aria-label="Try form"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      {children}
    </form>
  );
}

const local = () => new File([new Uint8Array(PNG)], "mine.png", { type: "image/png" });
const chooseLocal = () => {
  const input = document.querySelector("input[data-media-file-input]") as HTMLInputElement;
  fireEvent.change(input, { target: { files: [local()] } });
};

async function startHeldPick(harness: React.ReactElement, onSubmit = vi.fn()) {
  const served = route(undefined, true);
  vi.stubGlobal("fetch", served.mock);
  render(<InForm onSubmit={onSubmit}>{harness}</InForm>);
  await openAndPick();
  expect((await screen.findByRole("status")).textContent).toContain("Adding picture");
  await waitFor(() => expect(served.signals.length).toBeGreaterThan(1));
  return { served, onSubmit };
}

async function expectEnded(served: ReturnType<typeof route>, onSubmit: () => void) {
  expect(screen.queryByText(/Adding picture/)).toBeNull();
  expect(served.signals.every((signal) => signal.aborted)).toBe(true);
  fireEvent.submit(screen.getByRole("form", { name: "Try form" }));
  expect(onSubmit).toHaveBeenCalledTimes(1);
}

test("choosing a file while a Library pick arrives ends the pick: no Adding…, Run is not blocked, the late pick changes nothing", async () => {
  const onFile = vi.fn();
  const { served, onSubmit } = await startHeldPick(<FileHarness onFile={onFile} />);
  chooseLocal();
  await waitFor(() => expect(onFile).toHaveBeenCalledTimes(1));
  served.release();
  await new Promise((resolve) => setTimeout(resolve, 20));
  await expectEnded(served, onSubmit);
  expect(onFile).toHaveBeenCalledTimes(1);
  expect((onFile.mock.calls[0][0] as File).name).toBe("mine.png");
  expect(screen.getByText("mine.png")).toBeTruthy();
});

test("choosing a file while a Library pick arrives ends the pick for a reference input too", async () => {
  const onValue = vi.fn();
  const { served, onSubmit } = await startHeldPick(<ReferenceHarness onValue={onValue} />);
  chooseLocal();
  await waitFor(() => expect(onValue).toHaveBeenCalledTimes(1));
  served.release();
  await new Promise((resolve) => setTimeout(resolve, 20));
  await expectEnded(served, onSubmit);
  expect(onValue).toHaveBeenCalledTimes(1);
});

test("Remove ends a Library pick that is arriving", async () => {
  const onFile = vi.fn();
  const { served, onSubmit } = await startHeldPick(<FileHarness onFile={onFile} />);
  fireEvent.click(screen.getByRole("button", { name: "Remove picture for Picture to change" }));
  served.release();
  await new Promise((resolve) => setTimeout(resolve, 20));
  await expectEnded(served, onSubmit);
  expect(onFile.mock.calls.filter(([file]) => file !== null)).toEqual([]);
});

test("typing a link in Advanced ends a Library pick that is arriving", async () => {
  const onValue = vi.fn();
  const { served, onSubmit } = await startHeldPick(<ReferenceHarness onValue={onValue} />);
  fireEvent.change(screen.getByPlaceholderText("https://…"), { target: { value: "https://example.test/a.png" } });
  served.release();
  await new Promise((resolve) => setTimeout(resolve, 20));
  await expectEnded(served, onSubmit);
  expect(onValue).toHaveBeenCalledTimes(1);
  expect(onValue).toHaveBeenCalledWith("https://example.test/a.png");
});

test("reopening the Library ends a pick that is arriving, and a late answer changes nothing", async () => {
  const onFile = vi.fn();
  const { served, onSubmit } = await startHeldPick(<FileHarness onFile={onFile} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  served.release();
  await new Promise((resolve) => setTimeout(resolve, 20));
  await waitFor(() => expect(screen.queryByText(/Adding picture/)).toBeNull());
  expect(served.signals.every((signal) => signal.aborted)).toBe(true);
  expect(onFile).not.toHaveBeenCalled();
  void onSubmit;
});

test("a second Library pick replaces the first; only the second lands", async () => {
  const onFile = vi.fn();
  const { served } = await startHeldPick(<FileHarness onFile={onFile} />);
  const first = [...served.signals];
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
  await waitFor(() => expect(served.signals.length).toBeGreaterThan(first.length));
  expect(first.every((signal) => signal.aborted)).toBe(true);
  served.release();
  await waitFor(() => expect(onFile).toHaveBeenCalledTimes(1));
});

test("leaving the page aborts a Library pick that is arriving", async () => {
  const onFile = vi.fn();
  const { served } = await startHeldPick(<FileHarness onFile={onFile} />);
  cleanup();
  expect(served.signals.every((signal) => signal.aborted)).toBe(true);
  served.release();
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(onFile).not.toHaveBeenCalled();
});

test("closing the Library drops the list's signed links", async () => {
  vi.stubGlobal("fetch", route().mock);
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  await screen.findByRole("button", { name: "A teal fish" });
  expect(document.querySelector(`img[src="${THUMB}"]`)).toBeTruthy();
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(document.querySelector(`img[src="${THUMB}"]`)).toBeNull();
});
