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
const PNG = [0x89, 0x50, 0x4e, 0x47];

const reply = (body: string, type: string, ok = true) =>
  ({
    ok,
    status: ok ? 200 : 502,
    headers: new Headers({ "content-type": type }),
    json: async () => JSON.parse(body),
    blob: async () => new Blob([body], { type }),
  }) as unknown as Response;

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

function route(file: Blob | string = new Blob([new Uint8Array(PNG)], { type: "image/png" })) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/me/api/media?")) return reply(JSON.stringify(library), "application/json");
    if (url.startsWith(`/me/api/media/${ID}/download`))
      return reply(JSON.stringify({ asset_id: ID, download_url: SIGNED }), "application/json");
    if (url === SIGNED)
      return typeof file === "string"
        ? reply(file, "text/html")
        : ({ ok: true, status: 200, headers: new Headers({ "content-type": file.type }), blob: async () => file }) as unknown as Response;
    return reply("{}", "application/json", false);
  });
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
  vi.stubGlobal("fetch", route());
  render(<FileHarness onFile={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Choose from Library" }));
  const tile = await screen.findByRole("button", { name: "A teal fish" });
  expect(tile.querySelector("img")?.getAttribute("src")).toBe(THUMB);
});

test("a file input gets the picture's bytes and type, not the download link's JSON", async () => {
  const fetchMock = route();
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
  expect(fetchMock).toHaveBeenCalledWith(SIGNED);
  expect(await screen.findByText("From your Library")).toBeTruthy();
});

test("a reference input gets the picture as a data URI, never a bare asset id", async () => {
  vi.stubGlobal("fetch", route());
  const onValue = vi.fn();
  render(<ReferenceHarness onValue={onValue} />);
  await openAndPick();
  await waitFor(() => expect(onValue).toHaveBeenCalledTimes(1));
  const value = onValue.mock.calls[0][0] as string;
  expect(value).toMatch(/^data:image\/png;base64,/);
  expect(value).not.toContain(ID);
});

test("an error page from the link is not added as a picture", async () => {
  vi.stubGlobal("fetch", route("<html>expired</html>"));
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await openAndPick();
  expect((await screen.findByRole("alert")).textContent).toContain("Could not add that picture from your Library");
  expect(onFile).not.toHaveBeenCalled();
});

test("a picture over the size limit is named, not sent", async () => {
  const big = new Blob([new Uint8Array(10 * 1024 * 1024 + 1)], { type: "image/png" });
  vi.stubGlobal("fetch", route(big));
  const onFile = vi.fn();
  render(<FileHarness onFile={onFile} />);
  await openAndPick();
  expect((await screen.findByRole("alert")).textContent).toContain("Choose one under 10 MB");
  expect(onFile).not.toHaveBeenCalled();
});
