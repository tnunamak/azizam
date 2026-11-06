// "Choose from Library" on a media input. The Library's download route answers
// with a short-lived signed link, not the picture, so a pick must read the file
// from that link and the request must carry the picture's own bytes.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { PICTURE_ITEM, RECORDING_ITEM, stubbedLibrary } from "../../../tests/helpers/library-stub";
import { MediaInput } from "./media-input";

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

test("an input that takes any file shows no Library button and sends no Library request", () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  render(<MediaInput id="anything" label="Attachment" kind={null} file={null} onFileChange={() => {}} />);
  expect(screen.getByRole("button", { name: "Choose a file" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Choose from Library" })).toBeNull();
  expect(document.querySelector("input[data-media-file-input]")?.getAttribute("accept")).toBeNull();
  expect(fetchMock).not.toHaveBeenCalled();
});

test("a kind the Library cannot hold shows no Library button", () => {
  render(<MediaInput id="scent" label="Pheromone" kind="pheromone" file={null} onFileChange={() => {}} />);
  expect(screen.getByRole("button", { name: "Choose a file" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Choose from Library" })).toBeNull();
});

test("an audio input lists the audio kind and gets an audio file", async () => {
  const library = stubbedLibrary([PICTURE_ITEM, RECORDING_ITEM]);
  vi.stubGlobal("fetch", vi.fn(library.impl));
  const onFile = vi.fn();
  render(<MediaInput id="clip" label="Audio file" kind="audio" file={null} onFileChange={onFile} />);
  expect(document.querySelector("input[data-media-file-input]")?.getAttribute("accept")).toBe("audio/*");
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  expect(await screen.findByRole("heading", { name: "Choose an audio clip from your Library" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "A teal fish" })).toBeNull();
  fireEvent.click(await screen.findByRole("button", { name: "A short spoken note" }));
  await waitFor(() => expect(onFile).toHaveBeenCalledTimes(1));
  expect((onFile.mock.calls[0][0] as File).type).toBe("audio/wav");
  expect(library.listed.every((url) => url.includes("kind=audio"))).toBe(true);
});

test("an input that takes some types of a kind filters the file dialog, the list and a chosen file", async () => {
  const library = stubbedLibrary([
    PICTURE_ITEM,
    { ...PICTURE_ITEM, assetId: `img_${"d".repeat(64)}`, mediaType: "image/gif", prompt: "A spinning gif" },
  ]);
  vi.stubGlobal("fetch", vi.fn(library.impl));
  const onFile = vi.fn();
  render(
    <MediaInput
      id="mesh"
      label="Picture"
      kind="image"
      types={["image/png", "image/jpeg", "image/webp"]}
      accepted="a PNG, JPEG or WebP picture"
      hint="PNG, JPEG or WebP."
      file={null}
      onFileChange={onFile}
    />,
  );
  expect(screen.getByText("PNG, JPEG or WebP.")).toBeTruthy();
  expect(document.querySelector("input[data-media-file-input]")?.getAttribute("accept")).toBe(
    "image/png,image/jpeg,image/webp",
  );
  fireEvent.change(document.querySelector("input[data-media-file-input]")!, {
    target: { files: [new File(["g"], "spin.gif", { type: "image/gif" })] },
  });
  expect(screen.getByRole("alert").textContent).toContain("That file is not a PNG, JPEG or WebP picture");
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  await screen.findByRole("button", { name: "A teal fish" });
  expect(screen.queryByRole("button", { name: "A spinning gif" })).toBeNull();
});

test("the From your Library label goes when a pasted or recorded file replaces the pick", async () => {
  vi.stubGlobal("fetch", route().mock);
  function Replaceable() {
    const [file, setFile] = useState<File | null>(null);
    return (
      <>
        <MediaInput id="picture" label="Picture" kind="image" file={file} onFileChange={setFile} />
        <button type="button" onClick={() => setFile(new File(["x"], "pasted.png", { type: "image/png" }))}>
          Paste
        </button>
      </>
    );
  }
  render(<Replaceable />);
  await openAndPick();
  expect(await screen.findByText("From your Library")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Paste" }));
  expect(screen.queryByText("From your Library")).toBeNull();
  expect(screen.getByText("pasted.png")).toBeTruthy();
});

test("a chosen file is one summary: name, source and size, with Replace and remove controls, in both modes", async () => {
  vi.stubGlobal("fetch", route().mock);
  function Both() {
    const [file, setFile] = useState<File | null>(null);
    const [value, setValue] = useState<string | undefined>(undefined);
    return (
      <>
        <MediaInput id="as-file" label="As file" kind="image" file={file} onFileChange={setFile} />
        <MediaInput id="as-reference" label="As reference" kind="image" value={value} onChange={setValue} />
      </>
    );
  }
  render(<Both />);
  const inputs = document.querySelectorAll<HTMLInputElement>("input[data-media-file-input]");
  const kb = new File([new Uint8Array(2048)], "fox.png", { type: "image/png" });
  fireEvent.change(inputs[0], { target: { files: [kb] } });
  fireEvent.change(inputs[1], { target: { files: [kb] } });
  await waitFor(() => expect(document.querySelectorAll('[data-slot="picked-summary"]').length).toBe(2));
  for (const summary of Array.from(document.querySelectorAll<HTMLElement>('[data-slot="picked-summary"]'))) {
    expect(summary.textContent).toContain("fox.png");
    expect(summary.textContent).toContain("2 KB");
    // Two controls: Replace and remove.
    expect(Array.from(summary.querySelectorAll("button")).map((button) => button.textContent)).toEqual(["Replace", ""]);
  }
  fireEvent.click(screen.getAllByRole("button", { name: "Remove fox.png" })[1]);
  expect(document.querySelectorAll('[data-slot="picked-summary"]').length).toBe(1);
});

test("a Library pick is named by the Library's title; the file keeps its real name; a local file keeps its own", async () => {
  vi.stubGlobal("fetch", route().mock);
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await openAndPick();
  const summary = await waitFor(() => {
    const found = document.querySelector<HTMLElement>('[data-slot="picked-summary"]');
    expect(found).not.toBeNull();
    return found!;
  });
  expect(summary.querySelector("p > span")?.textContent).toBe("A teal fish");
  expect(screen.getByText("From your Library")).toBeTruthy();
  expect(summary.textContent).toContain("PNG");
  expect(summary.textContent).toMatch(/\d+ KB/);
  // What is sent is still the file, under its own name and type.
  expect((onFile.mock.calls[0][0] as File).name).toBe("library-picture.png");
  expect((onFile.mock.calls[0][0] as File).type).toBe("image/png");
  // One remove control, named for what the person sees.
  expect(screen.getByRole("button", { name: "Remove A teal fish" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Remove A teal fish" }));
  fireEvent.change(document.querySelector("input[data-media-file-input]")!, {
    target: { files: [new File([new Uint8Array(1024)], "fox.png", { type: "image/png" })] },
  });
  const local = await screen.findByRole("button", { name: "Remove fox.png" });
  expect(local).toBeTruthy();
  expect(screen.queryByText("From your Library")).toBeNull();
  expect(document.querySelector('[data-slot="picked-summary"]')?.textContent).toContain("PNG · 1 KB");
});

test("the Library dialog: a caption under each picture, a duration under an audio clip, focus on the first tile", async () => {
  vi.stubGlobal("fetch", route({ file: [new Uint8Array(PNG)], listed: { prompt: "A teal fish that swims by the reef", duration_s: 7 } }).mock);
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  const tile = await screen.findByRole("button", { name: "A teal fish that swims by the reef" });
  // The caption takes up to two lines and carries the whole title on hover.
  const caption = tile.querySelector("span.line-clamp-2");
  expect(caption?.textContent).toBe("A teal fish that swims by the reef");
  expect(caption?.getAttribute("title")).toBe("A teal fish that swims by the reef");
  expect(tile.textContent).toContain("0:07");
  expect(tile.getAttribute("title")).toBe("A teal fish that swims by the reef");
  // No subtitle: the title says what the dialog is, and the list says how it is ordered.
  expect(screen.queryByText("Newest first.")).toBeNull();
  await waitFor(() => expect(document.activeElement).toBe(tile));
  // The close button sits in the header row, not over the title.
  const header = document.querySelector('[data-slot="library-picker-header"]')!;
  expect(header.contains(screen.getByRole("button", { name: "Close" }))).toBe(true);
});

test("an item with no duration shows no duration line", async () => {
  vi.stubGlobal("fetch", route().mock);
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  const tile = await screen.findByRole("button", { name: "A teal fish" });
  expect(tile.textContent).toBe("A teal fish");
});

const DAY = 86_400;
const NOW_S = Math.floor(Date.now() / 1000);
const memo = (letter: string, prompt: string, daysAgo: number) => ({
  ...RECORDING_ITEM,
  assetId: `aud_${letter.repeat(64)}`,
  prompt,
  seconds: 83,
  createdAt: NOW_S - daysAgo * DAY,
});

test("audio is a list of rows, each with the icon, the whole title, the length and the date", async () => {
  const sunday = memo("c", "Voice memo: groceries for Sunday dinner, and the long list of things to do before the guests arrive", 2);
  const monday = memo("d", "Voice memo: groceries for Monday lunch", 4);
  const library = stubbedLibrary([sunday, monday]);
  vi.stubGlobal("fetch", vi.fn(library.impl));
  render(<MediaInput id="clip" label="Audio file" kind="audio" file={null} onFileChange={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  const first = await screen.findByRole("button", { name: sunday.prompt.slice(0, 80) });
  const second = await screen.findByRole("button", { name: monday.prompt });
  const list = first.closest("ul")!;
  expect(list.className).toContain("flex-col");
  expect(list.className).not.toContain("grid");
  for (const row of [first, second]) {
    expect(row.querySelector("svg")).not.toBeNull();
    expect(row.querySelector("img, video")).toBeNull();
    expect(row.textContent).toContain("1:23");
    expect(row.textContent).toMatch(/\b[A-Z][a-z]{2} \d{1,2}\b/);
    const title = row.querySelector("span.line-clamp-2")!;
    expect(title.getAttribute("title")).toBe(row.getAttribute("aria-label"));
  }
  // Two clips that begin alike are told apart by the end of the title, and neither is cut to one line.
  expect(first.querySelector("span.line-clamp-2")?.textContent).toContain("Sunday dinner");
  expect(second.querySelector("span.line-clamp-2")?.textContent).toContain("Monday lunch");
  expect(first.querySelector("span.truncate")).toBeNull();
});

test("a picture or a video is a grid; a kind with nothing to look at is a list", async () => {
  const library = stubbedLibrary([PICTURE_ITEM, { ...RECORDING_ITEM, kind: "model", mediaType: "model/gltf-binary", prompt: "A chair" }]);
  vi.stubGlobal("fetch", vi.fn(library.impl));
  render(<MediaInput id="picture" label="Picture" kind="image" file={null} onFileChange={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  const tile = await screen.findByRole("button", { name: "A teal fish" });
  expect(tile.closest("ul")!.className).toContain("grid-cols-3");
  // Five to a row from lg, so a desktop dialog shows more at once.
  expect(tile.closest("ul")!.className).toContain("lg:grid-cols-5");
  cleanup();
  vi.stubGlobal("fetch", vi.fn(library.impl));
  render(<MediaInput id="solid" label="Model" kind="model" file={null} onFileChange={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  const row = await screen.findByRole("button", { name: "A chair" });
  expect(row.closest("ul")!.className).toContain("flex-col");
});

test("the header shows an edge once the tiles have scrolled under it", async () => {
  vi.stubGlobal("fetch", route().mock);
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  await screen.findByRole("button", { name: "A teal fish" });
  const header = document.querySelector<HTMLElement>('[data-slot="library-picker-header"]')!;
  const body = document.querySelector<HTMLElement>('[data-slot="library-picker-body"]')!;
  expect(header.hasAttribute("data-scrolled")).toBe(false);
  body.scrollTop = 40;
  fireEvent.scroll(body);
  expect(header.hasAttribute("data-scrolled")).toBe(true);
  body.scrollTop = 0;
  fireEvent.scroll(body);
  expect(header.hasAttribute("data-scrolled")).toBe(false);
});

test("a picked Library clip shows its length between the type and the size", async () => {
  const library = stubbedLibrary([{ ...RECORDING_ITEM, seconds: 3 }]);
  vi.stubGlobal("fetch", vi.fn(library.impl));
  function AudioHarness() {
    const [file, setFile] = useState<File | null>(null);
    return <MediaInput id="clip" label="Audio file" kind="audio" file={file} onFileChange={setFile} />;
  }
  render(<AudioHarness />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A short spoken note" }));
  const summary = await waitFor(() => {
    const found = document.querySelector<HTMLElement>('[data-slot="picked-summary"]');
    expect(found).not.toBeNull();
    return found!;
  });
  expect(summary.textContent).toMatch(/From your Library.*WAV · 0:03 · \d+(\.\d+)? [KM]B/);
});

test("Replace opens the Library again for a Library pick, and a new pick takes the old one's place", async () => {
  const library = stubbedLibrary([
    PICTURE_ITEM,
    { ...PICTURE_ITEM, assetId: `img_${"e".repeat(64)}`, prompt: "A red kite" },
  ]);
  vi.stubGlobal("fetch", vi.fn(library.impl));
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await openAndPick();
  fireEvent.click(await screen.findByRole("button", { name: "Replace A teal fish" }));
  expect(await screen.findByRole("heading", { name: "Choose a picture from your Library" })).toBeTruthy();
  // Leaving the dialog keeps the choice.
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.getByRole("button", { name: "Remove A teal fish" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Replace A teal fish" }));
  fireEvent.click(await screen.findByRole("button", { name: "A red kite" }));
  expect(await screen.findByRole("button", { name: "Remove A red kite" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Remove A teal fish" })).toBeNull();
  expect(onFile).toHaveBeenLastCalledWith(expect.any(File));
});

test("Replace on a file from disk opens the file dialog again", () => {
  vi.stubGlobal("fetch", route().mock);
  render(<FileHarness onFile={() => {}} />);
  const input = document.querySelector<HTMLInputElement>("input[data-media-file-input]")!;
  fireEvent.change(input, { target: { files: [new File([new Uint8Array(1024)], "fox.png", { type: "image/png" })] } });
  const click = vi.spyOn(input, "click");
  return screen.findByRole("button", { name: "Replace fox.png" }).then((replace) => {
    fireEvent.click(replace);
    expect(click).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

// ---- Round 5: limits, cancelling, focus, labels, targets, search, the current pick, the phone sheet ----

const sized = (name: string, type: string, bytes: number) => {
  const file = new File([new Uint8Array(8)], name, { type });
  Object.defineProperty(file, "size", { value: bytes });
  return file;
};
const fileField = () => document.querySelector("input[data-media-file-input]") as HTMLInputElement;
const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

function AudioFileHarness({ onFile }: { onFile: (file: File | null) => void }) {
  const [file, setFile] = useState<File | null>(null);
  return (
    <MediaInput
      id="audio"
      label="Audio file"
      kind="audio"
      file={file}
      onFileChange={(next) => {
        setFile(next);
        onFile(next);
      }}
    />
  );
}

test("Try transcription takes a 30 MB audio file, and an untyped file input takes 11 MB", async () => {
  vi.stubGlobal("fetch", route().mock);
  const onAudio = vi.fn();
  render(<AudioFileHarness onFile={onAudio} />);
  // No size line promises a limit the gateway does not have.
  expect(screen.queryByText(/up to 10 MB/)).toBeNull();
  fireEvent.change(fileField(), { target: { files: [sized("talk.mp3", "audio/mpeg", 30 * MB)] } });
  expect(await screen.findByRole("button", { name: "Remove talk.mp3" })).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(onAudio).toHaveBeenCalledTimes(1);
  cleanup();
  const onAny = vi.fn();
  function AnyFile() {
    const [file, setFile] = useState<File | null>(null);
    return (
      <MediaInput
        id="any"
        label="File"
        kind={null}
        file={file}
        onFileChange={(next) => {
          setFile(next);
          onAny(next);
        }}
      />
    );
  }
  render(<AnyFile />);
  fireEvent.change(fileField(), { target: { files: [sized("data.bin", "application/octet-stream", 11 * MB)] } });
  expect(await screen.findByRole("button", { name: "Remove data.bin" })).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(onAny).toHaveBeenCalledTimes(1);
});

test("a reference input holds its picture inside the request, so 10 MB stays its limit and its hint says so", async () => {
  vi.stubGlobal("fetch", route().mock);
  const onValue = vi.fn();
  render(<ReferenceHarness onValue={onValue} />);
  expect(screen.getByText("Pictures up to 10 MB.")).toBeTruthy();
  fireEvent.change(fileField(), { target: { files: [sized("big.png", "image/png", 11 * MB)] } });
  expect(screen.getByRole("alert").textContent).toContain("Choose one under 10 MB");
  expect(onValue).not.toHaveBeenCalled();
});

test("a limit the screen passes applies to a file input", () => {
  render(<MediaInput id="capped" label="File" kind="audio" maxBytes={2 * MB} file={null} onFileChange={() => {}} />);
  expect(screen.getByText("Audio clips up to 2.0 MB.")).toBeTruthy();
  fireEvent.change(fileField(), { target: { files: [sized("long.mp3", "audio/mpeg", 3 * MB)] } });
  expect(screen.getByRole("alert").textContent).toContain("Choose one under 2.0 MB");
});

test("a recording made while a Library pick arrives ends the pick (Try's Record)", async () => {
  const onFile = vi.fn();
  function WithRecord() {
    const [file, setFile] = useState<File | null>(null);
    return (
      <>
        <MediaInput
          id="audio"
          label="Audio file"
          kind="audio"
          file={file}
          onFileChange={(next) => {
            setFile(next);
            onFile(next);
          }}
        />
        <button type="button" onClick={() => setFile(new File(["rec"], "recording.webm", { type: "audio/webm" }))}>
          Record
        </button>
      </>
    );
  }
  const library = stubbedLibrary([RECORDING_ITEM]);
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/download")) await held;
      return library.impl(input, init);
    }),
  );
  render(<WithRecord />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A short spoken note" }));
  expect((await screen.findByRole("status")).textContent).toContain("Adding audio");
  fireEvent.click(screen.getByRole("button", { name: "Record" }));
  await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  release();
  await settle();
  expect(screen.getByRole("button", { name: "Remove recording.webm" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Remove A short spoken note" })).toBeNull();
  expect(onFile).not.toHaveBeenCalled();
});

test("focus follows a person's own choice: Replace after a pick, the way to choose after Remove, never on arrival", async () => {
  vi.stubGlobal("fetch", route().mock);
  render(<FileHarness onFile={() => {}} />);
  // Drawn, not touched: nothing took focus.
  expect(document.activeElement).toBe(document.body);
  await openAndPick();
  const replace = await screen.findByRole("button", { name: "Replace A teal fish" });
  await waitFor(() => expect(document.activeElement).toBe(replace));
  // One more Enter must not discard what was just chosen.
  expect(document.activeElement).not.toBe(screen.getByRole("button", { name: "Remove A teal fish" }));
  fireEvent.click(screen.getByRole("button", { name: "Remove A teal fish" }));
  const choose = await screen.findByRole("button", { name: "Choose a file" });
  await waitFor(() => expect(document.activeElement).toBe(choose));
});

test("focus goes to Replace after a file from disk too, and a screen drawn with a file already chosen takes none", async () => {
  vi.stubGlobal("fetch", route().mock);
  render(<FileHarness onFile={() => {}} />);
  fireEvent.change(fileField(), { target: { files: [new File([new Uint8Array(1024)], "fox.png", { type: "image/png" })] } });
  const replace = await screen.findByRole("button", { name: "Replace fox.png" });
  await waitFor(() => expect(document.activeElement).toBe(replace));
  cleanup();
  render(
    <MediaInput
      id="p"
      label="Picture"
      kind="image"
      file={new File([new Uint8Array(1024)], "kept.png", { type: "image/png" })}
      onFileChange={() => {}}
    />,
  );
  await screen.findByRole("button", { name: "Replace kept.png" });
  expect(document.activeElement).toBe(document.body);
});

test("the choose button says when the input is required and when it is invalid", () => {
  render(<MediaInput id="r" label="Picture" kind="image" required invalid file={null} onFileChange={() => {}} />);
  const button = screen.getByRole("button", { name: "Choose a file" });
  expect(button.getAttribute("aria-required")).toBe("true");
  expect(button.getAttribute("aria-invalid")).toBe("true");
  cleanup();
  render(<MediaInput id="r" label="Picture" kind="image" file={null} onFileChange={() => {}} />);
  expect(screen.getByRole("button", { name: "Choose a file" }).hasAttribute("aria-required")).toBe(false);
});

test("the summary keeps its controls in a group beside the text, and its targets are 40 px (44 on a phone)", async () => {
  vi.stubGlobal("fetch", route().mock);
  render(<FileHarness onFile={() => {}} />);
  await openAndPick();
  const summary = (await waitFor(() => {
    const found = document.querySelector<HTMLElement>('[data-slot="picked-summary"]');
    expect(found).not.toBeNull();
    return found!;
  })) as HTMLElement;
  const replace = screen.getByRole("button", { name: "Replace A teal fish" });
  const remove = screen.getByRole("button", { name: "Remove A teal fish" });
  // The row answers to its own width, not the window's: under 28 rem the controls drop under the facts, in the
  // name's column; from 28 rem up they are a group on the right. The name's column takes all the free width.
  const grid = summary.firstElementChild as HTMLElement;
  expect(summary.className).toContain("@container");
  expect(grid.className).toContain("grid-cols-[4rem_minmax(0,1fr)]");
  expect(grid.className).toContain("@md:grid-cols-[4rem_minmax(0,1fr)_auto]");
  const text = summary.querySelector("p")!;
  expect(text.className).toContain("min-w-0");
  expect(text.className).not.toMatch(/(^|\s)(w-|basis-)/);
  // Replace and remove sit together in one group that is a sibling of the text, never inside it.
  expect(replace.parentElement).toBe(remove.parentElement);
  expect(replace.parentElement!.parentElement).toBe(grid);
  expect(text.contains(replace)).toBe(false);
  expect(replace.parentElement!.className).toContain("col-start-2 row-start-2");
  expect(replace.parentElement!.className).toContain("@md:col-start-3 @md:row-start-1");
  expect(remove.className).toContain("size-10");
  expect(remove.className).toContain("max-sm:size-11");
  expect(replace.className).toContain("min-h-10");
  expect(replace.className).toContain("max-sm:min-h-11");
});

const FIVE_ITEMS = [
  PICTURE_ITEM,
  { ...PICTURE_ITEM, assetId: `img_${"1".repeat(64)}`, prompt: "A red kite" },
  { ...PICTURE_ITEM, assetId: `img_${"2".repeat(64)}`, prompt: "A blue whale" },
  { ...PICTURE_ITEM, assetId: `img_${"3".repeat(64)}`, prompt: "Kite over the sea" },
];

test("the dialog's search filters the loaded items by title, and says so when nothing matches", async () => {
  vi.stubGlobal("fetch", vi.fn(stubbedLibrary(FIVE_ITEMS).impl));
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  await screen.findByRole("button", { name: "A teal fish" });
  const search = screen.getByRole("searchbox", { name: "Search your pictures" });
  // Keyboard-reachable: a real field, in the header, before the tiles in tab order.
  expect(document.querySelector('[data-slot="library-picker-header"]')!.contains(search)).toBe(true);
  fireEvent.change(search, { target: { value: "KITE" } });
  expect(screen.getByRole("button", { name: "A red kite" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Kite over the sea" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "A teal fish" })).toBeNull();
  expect(screen.queryByRole("button", { name: "A blue whale" })).toBeNull();
  fireEvent.change(search, { target: { value: "zebra" } });
  expect(screen.getByRole("status").textContent).toBe("No pictures match “zebra”.");
  expect(screen.queryAllByRole("button", { name: /A teal fish|A red kite/ })).toEqual([]);
  fireEvent.change(search, { target: { value: "" } });
  expect(screen.getByRole("button", { name: "A teal fish" })).toBeTruthy();
  // A filtered item is still picked as itself.
  fireEvent.change(search, { target: { value: "whale" } });
  fireEvent.click(screen.getByRole("button", { name: "A blue whale" }));
  expect(await screen.findByRole("button", { name: "Remove A blue whale" })).toBeTruthy();
});

test("the search starts empty each time the dialog opens, and an empty Library offers none", async () => {
  vi.stubGlobal("fetch", vi.fn(stubbedLibrary(FIVE_ITEMS).impl));
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  await screen.findByRole("button", { name: "A teal fish" });
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "kite" } });
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  await screen.findByRole("button", { name: "A teal fish" });
  expect((screen.getByRole("searchbox") as HTMLInputElement).value).toBe("");
  cleanup();
  vi.stubGlobal("fetch", vi.fn(stubbedLibrary([]).impl));
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  await screen.findByText(/in your Library yet/);
  expect(screen.queryByRole("searchbox")).toBeNull();
});

test("Replace reopens the Library with the current pick marked, scrolled into view and focused", async () => {
  const scrolled: string[] = [];
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: function scrollIntoView(this: HTMLElement) {
      scrolled.push(this.getAttribute("aria-label") ?? "");
    },
  });
  vi.stubGlobal("fetch", vi.fn(stubbedLibrary(FIVE_ITEMS).impl));
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A blue whale" }));
  fireEvent.click(await screen.findByRole("button", { name: "Replace A blue whale" }));
  const current = await screen.findByRole("button", { name: "A blue whale" });
  // Marked for a screen reader and for the eye, and nothing else is.
  expect(current.getAttribute("aria-current")).toBe("true");
  expect(current.querySelector('[data-slot="library-current-mark"]')).not.toBeNull();
  expect(document.querySelectorAll('[aria-current="true"]')).toHaveLength(1);
  expect(document.querySelectorAll('[data-slot="library-current-mark"]')).toHaveLength(1);
  await waitFor(() => expect(document.activeElement).toBe(current));
  expect(scrolled).toContain("A blue whale");
  // A first pick (nothing held) marks nothing and focuses the first tile.
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "Remove A blue whale" }));
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  const first = await screen.findByRole("button", { name: "A teal fish" });
  expect(document.querySelectorAll('[aria-current="true"]')).toHaveLength(0);
  await waitFor(() => expect(document.activeElement).toBe(first));
});

test("an audio row marks the current pick too", async () => {
  vi.stubGlobal("fetch", vi.fn(stubbedLibrary([RECORDING_ITEM, { ...RECORDING_ITEM, assetId: `aud_${"4".repeat(64)}`, prompt: "A second note" }]).impl));
  render(<AudioFileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A second note" }));
  fireEvent.click(await screen.findByRole("button", { name: "Replace A second note" }));
  const row = await screen.findByRole("button", { name: "A second note" });
  expect(row.getAttribute("aria-current")).toBe("true");
  expect(row.querySelector('[data-slot="library-current-mark"]')).not.toBeNull();
  expect(screen.getByRole("button", { name: "A short spoken note" }).hasAttribute("aria-current")).toBe(false);
});

test("the dialog: 40 px close, a darker pointer hover, one focus ring, and no subtitle", async () => {
  vi.stubGlobal("fetch", vi.fn(stubbedLibrary([RECORDING_ITEM]).impl));
  render(<AudioFileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  const row = await screen.findByRole("button", { name: "A short spoken note" });
  const close = screen.getByRole("button", { name: "Close" });
  expect(close.className).toContain("size-10");
  expect(close.className).toContain("max-sm:size-11");
  for (const control of [close, row]) {
    expect(control.className).toContain("focus-visible:ring-2");
    expect(control.className).toContain("focus-visible:ring-offset-2");
    expect(control.className).toContain("focus-visible:ring-focus-ring");
    expect(control.className).toContain("cursor-pointer");
  }
  // The hover fill is the scroll-edge tone, not the 1.05:1 wash it was.
  expect(row.className).toContain("hover:bg-border");
  expect(row.className).not.toMatch(/hover:bg-muted/);
  expect(screen.queryByText("Newest first.")).toBeNull();
});

test("under 640 px the picker is a full-height bottom sheet, and its bottom edge shows while more is below", async () => {
  vi.stubGlobal("fetch", vi.fn(stubbedLibrary(FIVE_ITEMS).impl));
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  await screen.findByRole("button", { name: "A teal fish" });
  const sheet = screen.getByRole("dialog");
  for (const phone of [
    "max-sm:bottom-0",
    "max-sm:top-auto",
    "max-sm:left-0",
    "max-sm:max-w-none",
    "max-sm:translate-y-0",
    "max-sm:rounded-b-none",
    "max-sm:h-[calc(100dvh-1rem)]",
  ]) {
    expect(sheet.className).toContain(phone);
  }
  // The header is outside the scrolling body, so it stays put.
  const header = document.querySelector<HTMLElement>('[data-slot="library-picker-header"]')!;
  const body = document.querySelector<HTMLElement>('[data-slot="library-picker-body"]')!;
  expect(body.contains(header)).toBe(false);
  expect(body.className).toContain("overflow-y-auto");
  // The bottom edge is on the desktop dialog as well as the phone sheet.
  expect(body.className).toContain("data-[more-below]:shadow-");
  expect(body.className).not.toContain("max-sm:data-[more-below]");
  // More list below the fold: the edge shows; scrolled to the end: it goes.
  const metrics = { scrollHeight: 900, clientHeight: 400, scrollTop: 0 };
  for (const key of ["scrollHeight", "clientHeight", "scrollTop"] as const) {
    Object.defineProperty(body, key, { configurable: true, get: () => metrics[key], set: (value) => (metrics[key] = value) });
  }
  fireEvent.scroll(body);
  expect(body.hasAttribute("data-more-below")).toBe(true);
  metrics.scrollTop = 500;
  fireEvent.scroll(body);
  expect(body.hasAttribute("data-more-below")).toBe(false);
});

// ---- Round 6 ----

test("the current pick's tile has a white-ringed check and a dark teal border; the other tiles have neither", async () => {
  vi.stubGlobal("fetch", vi.fn(stubbedLibrary(FIVE_ITEMS).impl));
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A blue whale" }));
  fireEvent.click(await screen.findByRole("button", { name: "Replace A blue whale" }));
  const current = await screen.findByRole("button", { name: "A blue whale" });
  const mark = current.querySelector('[data-slot="library-current-mark"]') as HTMLElement;
  // The ring is what keeps the disc apart from a dark picture; it is 2 px and white.
  expect(mark.className).toContain("ring-2");
  expect(mark.className).toContain("ring-white");
  const frame = current.querySelector("img")!.parentElement!;
  expect(frame.className).toContain("border-primary");
  expect(frame.className).toContain("border-2");
  const other = screen.getByRole("button", { name: "A red kite" }).querySelector("img")!.parentElement!;
  expect(other.className).not.toContain("border-primary");
});

test("a tile's label has inner padding, so its first letter clears the focus ring", async () => {
  vi.stubGlobal("fetch", route().mock);
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  const label = (await screen.findByRole("button", { name: "A teal fish" })).querySelector("span.line-clamp-2")!;
  expect(label.className).toContain("px-1.5");
});

/** A mouse click on the "A teal fish" tile at a spot, the way a person's pointer does it (a double-click has detail 2). */
async function mouseClickTile(at: { clientX: number; clientY: number }) {
  fireEvent.click(await screen.findByRole("button", { name: "Choose from Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }), { detail: 2, ...at });
}

test("the second click of a double-click on a tile does not remove the pick that has taken its place", async () => {
  vi.stubGlobal("fetch", route().mock);
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await mouseClickTile({ clientX: 300, clientY: 400 });
  const remove = await screen.findByRole("button", { name: "Remove A teal fish" });
  // The same spot, as a double-click's second click is, a moment later.
  fireEvent.click(remove, { detail: 2, clientX: 302, clientY: 399 });
  expect(screen.getByRole("button", { name: "Remove A teal fish" })).toBeTruthy();
  expect(onFile).not.toHaveBeenCalledWith(null);
});

test("a click on Remove somewhere else, or after the moment has passed, is the person's own", async () => {
  vi.stubGlobal("fetch", route().mock);
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await mouseClickTile({ clientX: 300, clientY: 400 });
  // Elsewhere on the screen: not the tail of that double-click.
  fireEvent.click(await screen.findByRole("button", { name: "Remove A teal fish" }), { detail: 1, clientX: 700, clientY: 90 });
  await waitFor(() => expect(onFile).toHaveBeenLastCalledWith(null));
  // The same spot, once the moment has passed.
  await mouseClickTile({ clientX: 300, clientY: 400 });
  const now = performance.now();
  const clock = vi.spyOn(performance, "now").mockReturnValue(now + 1000);
  try {
    fireEvent.click(await screen.findByRole("button", { name: "Remove A teal fish" }), { detail: 1, clientX: 300, clientY: 400 });
  } finally {
    clock.mockRestore();
  }
  await waitFor(() => expect(screen.queryByRole("button", { name: "Remove A teal fish" })).toBeNull());
});

test("Replace is never ignored, and a key press on Remove right after a pick removes it, even where the pointer was", async () => {
  vi.stubGlobal("fetch", route().mock);
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await mouseClickTile({ clientX: 300, clientY: 400 });
  // Replace on the same spot, at once: it reopens the Library.
  fireEvent.click(await screen.findByRole("button", { name: "Replace A teal fish" }), { detail: 1, clientX: 300, clientY: 400 });
  expect(await screen.findByRole("dialog")).toBeTruthy();
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  // Enter or Space on a focused button is a click with no detail, and has no position: it is a choice.
  fireEvent.click(screen.getByRole("button", { name: "Replace A teal fish" }));
  fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }), { detail: 2, clientX: 300, clientY: 400 });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  fireEvent.click(await screen.findByRole("button", { name: "Remove A teal fish" }), { detail: 0 });
  await waitFor(() => expect(onFile).toHaveBeenLastCalledWith(null));
});

test("the search field has the shared focus ring, hides the browser's clear button and has its own", async () => {
  vi.stubGlobal("fetch", vi.fn(stubbedLibrary(FIVE_ITEMS).impl));
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  await screen.findByRole("button", { name: "A teal fish" });
  const search = screen.getByRole("searchbox", { name: "Search your pictures" });
  expect(search.className).toContain("focus-visible:ring-focus-ring");
  expect(search.className).toContain("focus-visible:ring-2");
  // No second, lighter teal edge beside the ring.
  expect(search.className).toContain("focus-visible:border-input!");
  expect(search.className).toContain("[&::-webkit-search-cancel-button]:appearance-none");
  // Nothing typed, nothing to clear.
  expect(screen.queryByRole("button", { name: "Clear search" })).toBeNull();
  fireEvent.change(search, { target: { value: "kite" } });
  const clear = screen.getByRole("button", { name: "Clear search" });
  expect(clear.className).toContain("size-10");
  expect(clear.className).toContain("max-sm:size-11");
  expect(clear.className).toContain("focus-visible:ring-focus-ring");
  fireEvent.click(clear);
  expect((search as HTMLInputElement).value).toBe("");
  expect(screen.queryByRole("button", { name: "Clear search" })).toBeNull();
  expect(document.activeElement).toBe(search);
  expect(screen.getByRole("button", { name: "A teal fish" })).toBeTruthy();
});

test("the desktop dialog's top edge is fixed, not centred, so the search field does not move as results shrink", async () => {
  vi.stubGlobal("fetch", route().mock);
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  await screen.findByRole("button", { name: "A teal fish" });
  const dialog = screen.getByRole("dialog");
  expect(dialog.className).toContain("sm:top-[12vh]");
  expect(dialog.className).toContain("sm:translate-y-0");
  expect(dialog.className).toContain("sm:max-h-[80vh]");
});
