// The behaviour every media input shares: choose a file, take a file handed over (a drop or a paste),
// choose from the Library. Each screen draws its own controls; these specs draw the bare minimum.
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { PICTURE_ITEM, RECORDING_ITEM, stubbedLibrary } from "../../../tests/helpers/library-stub";
import { LibraryPicker } from "./library-picker";
import { LIBRARY_SOURCE, useMediaPick, type MediaPickOptions } from "./use-media-pick";

type Harness = Partial<MediaPickOptions<string>> & {
  onCommit?: (file: File, prepared: string | undefined) => void;
  heldFile?: File | null;
};

let latest: ReturnType<typeof useMediaPick<string>>;

function Probe(props: Harness) {
  const [held, setHeld] = useState<File | null>(props.heldFile ?? null);
  const pick = useMediaPick<string>({
    types: ["image/*"],
    current: held,
    commit: (file, prepared) => {
      setHeld(file);
      props.onCommit?.(file, prepared);
    },
    ...props,
  });
  latest = pick;
  return (
    <form>
      <input ref={pick.fileInput} type="file" accept={pick.accept} data-testid="file" onChange={pick.onFileInputChange} />
      <button type="button" onClick={pick.openLibrary}>Library</button>
      <button type="button" onClick={() => setHeld(new File(["x"], "pasted.png", { type: "image/png" }))}>Replace</button>
      <button type="button" onClick={() => setHeld(null)}>Remove</button>
      <output data-testid="held">{held?.name ?? ""}</output>
      <output data-testid="source">{pick.source ?? ""}</output>
      <output data-testid="pending">{String(pick.pending)}</output>
      {pick.error ? <p role="alert">{pick.error}</p> : null}
      {pick.libraryAvailable && pick.kind ? (
        <LibraryPicker
          open={pick.libraryOpen}
          onOpenChange={pick.setLibraryOpen}
          kind={pick.kind}
          types={pick.types}
          onPick={(item) => void pick.pickFromLibrary(item)}
        />
      ) : null}
    </form>
  );
}

const png = (name = "fox.png", size = 4) => new File([new Uint8Array(size)], name, { type: "image/png" });

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", { configurable: true, value: () => false });
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: () => {} });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("a file that fits is committed at once, with no pending state", async () => {
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} />);
  const file = png();
  fireEvent.change(screen.getByTestId("file"), { target: { files: [file] } });
  expect(onCommit).toHaveBeenCalledWith(file, undefined);
  await waitFor(() => expect(screen.getByTestId("source")).toHaveTextContent("fox.png"));
  expect(screen.getByTestId("pending")).toHaveTextContent("false");
});

test("a file of another type is refused with the words for what is wanted, and is not committed", () => {
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} />);
  fireEvent.change(screen.getByTestId("file"), {
    target: { files: [new File(["x"], "notes.txt", { type: "text/plain" })] },
  });
  expect(screen.getByRole("alert")).toHaveTextContent("That file is not a picture. Choose a picture file.");
  expect(onCommit).not.toHaveBeenCalled();
});

test("only the listed types of a kind are taken, and the error names them", () => {
  const onCommit = vi.fn();
  render(
    <Probe
      onCommit={onCommit}
      types={["image/png", "image/jpeg", "image/webp"]}
      accepted="a PNG, JPEG or WebP picture"
    />,
  );
  fireEvent.change(screen.getByTestId("file"), {
    target: { files: [new File(["x"], "spin.gif", { type: "image/gif" })] },
  });
  expect(screen.getByRole("alert")).toHaveTextContent("That file is not a PNG, JPEG or WebP picture.");
  expect(onCommit).not.toHaveBeenCalled();
  fireEvent.change(screen.getByTestId("file"), { target: { files: [png()] } });
  expect(onCommit).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("alert")).toBeNull();
});

test("a file over the limit is refused with its size", () => {
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} maxBytes={1024 * 1024} />);
  fireEvent.change(screen.getByTestId("file"), { target: { files: [png("big.png", 2 * 1024 * 1024)] } });
  expect(screen.getByRole("alert")).toHaveTextContent("That picture is 2.0 MB. Choose one under 1.0 MB.");
  expect(onCommit).not.toHaveBeenCalled();
});

test("take() accepts a dropped or pasted file the same way the dialog does", async () => {
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} />);
  await act(async () => {
    await latest.take(new File(["x"], "dropped.txt", { type: "text/plain" }));
  });
  expect(onCommit).not.toHaveBeenCalled();
  await act(async () => {
    await latest.take(png("dropped.png"));
  });
  expect(onCommit).toHaveBeenCalledTimes(1);
});

test("a choice that takes time is pending, and a newer choice supersedes it", async () => {
  const releases: Array<(value: string) => void> = [];
  const onCommit = vi.fn();
  render(
    <Probe
      onCommit={onCommit}
      prepare={() => new Promise<string>((resolve) => releases.push(resolve))}
    />,
  );
  fireEvent.change(screen.getByTestId("file"), { target: { files: [png("old.png")] } });
  expect(screen.getByTestId("pending")).toHaveTextContent("true");
  fireEvent.change(screen.getByTestId("file"), { target: { files: [png("new.png")] } });
  await act(async () => releases[1]("new"));
  await act(async () => releases[0]("old"));
  expect(onCommit).toHaveBeenCalledTimes(1);
  expect((onCommit.mock.calls[0][0] as File).name).toBe("new.png");
  expect(onCommit.mock.calls[0][1]).toBe("new");
  expect(screen.getByTestId("pending")).toHaveTextContent("false");
});

test("a rejected file does not cancel a pending valid choice", async () => {
  const releases: Array<(value: string) => void> = [];
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} prepare={() => new Promise<string>((resolve) => releases.push(resolve))} />);
  fireEvent.change(screen.getByTestId("file"), { target: { files: [png()] } });
  fireEvent.change(screen.getByTestId("file"), {
    target: { files: [new File(["x"], "notes.txt", { type: "text/plain" })] },
  });
  await act(async () => releases[0]("valid"));
  expect(onCommit).toHaveBeenCalledTimes(1);
});

test("a read that fails says so and keeps nothing", async () => {
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} prepare={() => Promise.reject(new Error("boom"))} />);
  fireEvent.change(screen.getByTestId("file"), { target: { files: [png()] } });
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not read that picture. Try another file.");
  expect(onCommit).not.toHaveBeenCalled();
  expect(screen.getByTestId("pending")).toHaveTextContent("false");
});

test("leaving the page ends a pending choice", async () => {
  let release!: (value: string) => void;
  const onCommit = vi.fn();
  const view = render(<Probe onCommit={onCommit} prepare={() => new Promise<string>((resolve) => (release = resolve))} />);
  fireEvent.change(screen.getByTestId("file"), { target: { files: [png()] } });
  view.unmount();
  await act(async () => release("late"));
  expect(onCommit).not.toHaveBeenCalled();
});

test("Run is held while a choice arrives", async () => {
  let release!: (value: string) => void;
  const submitted = vi.fn((event: Event) => event.preventDefault());
  const view = render(<Probe prepare={() => new Promise<string>((resolve) => (release = resolve))} />);
  const form = view.container.querySelector("form")!;
  form.addEventListener("submit", submitted);
  fireEvent.change(screen.getByTestId("file"), { target: { files: [png()] } });
  fireEvent.submit(form);
  expect(submitted).not.toHaveBeenCalled();
  expect(latest.blockedSubmit).toBe(true);
  await act(async () => release("ok"));
  fireEvent.submit(form);
  expect(submitted).toHaveBeenCalledTimes(1);
});

test("a Library pick is committed as a file and labelled as from the Library", async () => {
  const library = stubbedLibrary([PICTURE_ITEM]);
  vi.stubGlobal("fetch", vi.fn(library.impl));
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} />);
  fireEvent.click(screen.getByRole("button", { name: "Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
  await waitFor(() => expect(onCommit).toHaveBeenCalledTimes(1));
  const file = onCommit.mock.calls[0][0] as File;
  expect(file.type).toBe("image/png");
  expect(library.listed[0]).toContain("kind=image");
  expect(screen.getByTestId("source")).toHaveTextContent(LIBRARY_SOURCE);
});

test("the source label is dropped when another route (a paste) replaces the file", async () => {
  vi.stubGlobal("fetch", vi.fn(stubbedLibrary([PICTURE_ITEM]).impl));
  render(<Probe />);
  fireEvent.click(screen.getByRole("button", { name: "Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
  await waitFor(() => expect(screen.getByTestId("source")).toHaveTextContent(LIBRARY_SOURCE));
  fireEvent.click(screen.getByRole("button", { name: "Replace" }));
  expect(screen.getByTestId("source")).toHaveTextContent("");
});

test("the Library lists only the kind the input takes, and only the types it takes", async () => {
  const library = stubbedLibrary([
    PICTURE_ITEM,
    { ...PICTURE_ITEM, assetId: `img_${"c".repeat(64)}`, mediaType: "image/gif", prompt: "A spinning gif" },
    RECORDING_ITEM,
  ]);
  vi.stubGlobal("fetch", vi.fn(library.impl));
  render(<Probe types={["image/png", "image/jpeg", "image/webp"]} />);
  fireEvent.click(screen.getByRole("button", { name: "Library" }));
  expect(await screen.findByRole("button", { name: "A teal fish" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "A spinning gif" })).toBeNull();
  expect(screen.queryByRole("button", { name: "A short spoken note" })).toBeNull();
  expect(library.listed.every((url) => url.includes("kind=image"))).toBe(true);
});

test("an input that takes any file, or a kind the Library cannot hold, offers no Library", () => {
  render(<Probe types={undefined} />);
  expect(latest.libraryAvailable).toBe(false);
  cleanup();
  render(<Probe types={["smell/*"]} />);
  expect(latest.kind).toBe("smell");
  expect(latest.libraryAvailable).toBe(false);
  cleanup();
  render(<Probe types={["audio/*"]} />);
  expect(latest.libraryAvailable).toBe(true);
});

test("a pick the Library list marks over the limit is refused before any download", async () => {
  const library = stubbedLibrary([PICTURE_ITEM]);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const response = await library.impl(input, init);
      if (String(input).startsWith("/me/api/media?")) {
        const body = (await response.json()) as { assets: Array<Record<string, unknown>> };
        return Response.json({ assets: body.assets.map((asset) => ({ ...asset, size_bytes: 50 * 1024 * 1024 })) });
      }
      return response;
    }),
  );
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} />);
  fireEvent.click(screen.getByRole("button", { name: "Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Choose one under 10 MB");
  expect(library.downloaded).toEqual([]);
  expect(onCommit).not.toHaveBeenCalled();
});

/** A file that reports `bytes` without holding them. */
function sized(name: string, type: string, bytes: number) {
  const file = new File([new Uint8Array(8)], name, { type });
  Object.defineProperty(file, "size", { value: bytes });
  return file;
}

const MB = 1024 * 1024;

test("a screen that keeps the file as it is has no size limit: 30 MB of audio and 11 MB of any file are taken", async () => {
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} types={["audio/*"]} />);
  await act(async () => {
    await latest.take(sized("talk.mp3", "audio/mpeg", 30 * MB));
  });
  expect(onCommit).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(latest.maxBytes).toBeUndefined();
  cleanup();
  const onAny = vi.fn();
  render(<Probe onCommit={onAny} types={undefined} />);
  await act(async () => {
    await latest.take(sized("notes.bin", "application/octet-stream", 11 * MB));
  });
  expect(onAny).toHaveBeenCalledTimes(1);
});

test("a screen that reads the file into the request is held to the shared limit; a limit the screen passes wins", async () => {
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} prepare={async () => "data:x"} />);
  await act(async () => {
    await latest.take(sized("big.png", "image/png", 11 * MB));
  });
  expect(screen.getByRole("alert")).toHaveTextContent("Choose one under 10 MB");
  expect(onCommit).not.toHaveBeenCalled();
  cleanup();
  render(<Probe onCommit={onCommit} prepare={async () => "data:x"} maxBytes={20 * MB} />);
  await act(async () => {
    await latest.take(sized("big.png", "image/png", 11 * MB));
  });
  expect(onCommit).toHaveBeenCalledTimes(1);
});

test("the Library download keeps its own limit when a file from the disk has none", async () => {
  const library = stubbedLibrary([PICTURE_ITEM]);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const response = await library.impl(input, init);
      if (String(input).startsWith("/me/api/media?")) {
        const body = (await response.json()) as { assets: Array<Record<string, unknown>> };
        return Response.json({ assets: body.assets.map((asset) => ({ ...asset, size_bytes: 11 * MB })) });
      }
      return response;
    }),
  );
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} />);
  expect(latest.maxBytes).toBeUndefined();
  fireEvent.click(screen.getByRole("button", { name: "Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Choose one under 10 MB");
  expect(onCommit).not.toHaveBeenCalled();
});

/** A Library whose download is held until `release()`. */
function heldLibrary() {
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  const base = stubbedLibrary([PICTURE_ITEM]).impl;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/download")) await held;
      return base(input, init);
    }),
  );
  return { release };
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

async function startHeldPick() {
  const library = heldLibrary();
  fireEvent.click(screen.getByRole("button", { name: "Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
  await waitFor(() => expect(screen.getByTestId("pending")).toHaveTextContent("true"));
  return library;
}

test("a paste or a recording that replaces the file ends a Library pick still arriving", async () => {
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} />);
  const library = await startHeldPick();
  // Another route (Try's Record, a paste) hands the screen a file of its own.
  fireEvent.click(screen.getByRole("button", { name: "Replace" }));
  await waitFor(() => expect(screen.getByTestId("pending")).toHaveTextContent("false"));
  library.release();
  await settle();
  expect(onCommit).not.toHaveBeenCalled();
  expect(screen.getByTestId("source")).toHaveTextContent("");
});

test("a Remove outside the hook (a chip) ends a Library pick that is replacing a file", async () => {
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} heldFile={png("old.png")} />);
  const library = await startHeldPick();
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  await waitFor(() => expect(screen.getByTestId("pending")).toHaveTextContent("false"));
  library.release();
  await settle();
  expect(onCommit).not.toHaveBeenCalled();
  expect(screen.getByTestId("held")).toHaveTextContent("");
});

test("a pick's own commit is not read as another route's change", async () => {
  const onCommit = vi.fn();
  render(<Probe onCommit={onCommit} heldFile={png("old.png")} />);
  const library = await startHeldPick();
  library.release();
  await waitFor(() => expect(onCommit).toHaveBeenCalledTimes(1));
  expect(screen.getByTestId("source")).toHaveTextContent(LIBRARY_SOURCE);
});

function ReferenceProbe() {
  const [value, setValue] = useState<string | null>(null);
  const pick = useMediaPick<string>({
    types: ["image/*"],
    prepare: async () => "data:image/png;base64,AAAA",
    currentReference: value,
    commit: (_file, reference) => setValue(reference),
  });
  latest = pick;
  return (
    <form>
      <button type="button" onClick={pick.openLibrary}>Library</button>
      <button type="button" onClick={() => setValue("https://example.test/typed.png")}>Type a link</button>
      <output data-testid="source">{pick.source ?? ""}</output>
      <output data-testid="title">{pick.sourceTitle ?? ""}</output>
      <output data-testid="pending">{String(pick.pending)}</output>
      <input ref={pick.fileInput} type="file" data-testid="file" onChange={pick.onFileInputChange} />
      {pick.libraryAvailable && pick.kind ? (
        <LibraryPicker
          open={pick.libraryOpen}
          onOpenChange={pick.setLibraryOpen}
          kind={pick.kind}
          types={pick.types}
          onPick={(item) => void pick.pickFromLibrary(item)}
        />
      ) : null}
    </form>
  );
}

test("a reference input forgets the Library title and source once its value is no longer the committed one", async () => {
  vi.stubGlobal("fetch", vi.fn(stubbedLibrary([PICTURE_ITEM]).impl));
  render(<ReferenceProbe />);
  fireEvent.click(screen.getByRole("button", { name: "Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
  await waitFor(() => expect(screen.getByTestId("source")).toHaveTextContent(LIBRARY_SOURCE));
  expect(screen.getByTestId("title")).toHaveTextContent("A teal fish");
  // The value changed in Advanced (or from outside): the reference is no longer the Library item.
  fireEvent.click(screen.getByRole("button", { name: "Type a link" }));
  expect(screen.getByTestId("source")).toHaveTextContent("");
  expect(screen.getByTestId("title")).toHaveTextContent("");
});

test("a reference input's pending Library pick ends when its value changes elsewhere", async () => {
  const library = heldLibrary();
  render(<ReferenceProbe />);
  fireEvent.click(screen.getByRole("button", { name: "Library" }));
  fireEvent.click(await screen.findByRole("button", { name: "A teal fish" }));
  await waitFor(() => expect(screen.getByTestId("pending")).toHaveTextContent("true"));
  fireEvent.click(screen.getByRole("button", { name: "Type a link" }));
  await waitFor(() => expect(screen.getByTestId("pending")).toHaveTextContent("false"));
  library.release();
  await settle();
  expect(screen.getByTestId("source")).toHaveTextContent("");
});
