import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { type AdapterUploaded } from "@/lib/adapter-upload";
import { AdapterUploadControl } from "./adapter-upload-control";

afterEach(cleanup);

const adapter: AdapterUploaded = {
  id: "asset_test",
  object: "gateway.asset",
  kind: "adapter",
  media_type: "adapter/safetensors",
  size_bytes: 5,
  architectures: [],
  offered: true,
};
const successText = "Added to your Library and ready to pick.";
const refusalText = "This file is not a supported style adapter.";
const ok = () => Response.json(adapter);
const refused = () =>
  Response.json({ error: { code: "invalid_adapter", message: refusalText } }, { status: 400 });

function button() {
  return screen.getByRole<HTMLButtonElement>("button", { name: "Upload your own style adapter" });
}

function fileInput() {
  return document.querySelector<HTMLInputElement>("input[data-adapter-file-input]")!;
}

function pick(name = "style.safetensors") {
  fireEvent.change(fileInput(), { target: { files: [new File(["style"], name)] } });
}

it("opens the hidden safetensors picker, then sends the chosen file and reports success once", async () => {
  const send = vi.fn(async ({ method, body }: { method: "POST"; body: FormData }) => {
    expect(method).toBe("POST");
    expect(body).toBeInstanceOf(FormData);
    const sent = body.get("file");
    expect(sent).toBeInstanceOf(File);
    expect((sent as File).name).toBe("style.safetensors");
    expect((sent as File).size).toBe(5);
    return ok();
  });
  const onUploaded = vi.fn();
  render(<AdapterUploadControl send={send} onUploaded={onUploaded} />);

  const input = fileInput();
  expect(input.hidden).toBe(true);
  expect(input.accept).toBe(".safetensors");
  const click = vi.spyOn(input, "click");
  fireEvent.click(button());
  expect(click).toHaveBeenCalledTimes(1);
  click.mockRestore();

  pick();
  const status = await screen.findByText(successText);
  expect(status.closest('[aria-live="polite"]')).not.toBeNull();
  expect(send).toHaveBeenCalledTimes(1);
  expect(onUploaded).toHaveBeenCalledTimes(1);
  expect(onUploaded).toHaveBeenCalledWith(adapter);
});

it("reports the server refusal in an alert without calling onUploaded", async () => {
  const onUploaded = vi.fn();
  render(<AdapterUploadControl send={async () => refused()} onUploaded={onUploaded} />);
  pick();
  expect((await screen.findByRole("alert")).textContent).toBe(refusalText);
  expect(onUploaded).not.toHaveBeenCalled();
});

it("disables the button while sending, then accepts another selection", async () => {
  let resolve!: (value: Response) => void;
  const send = vi.fn(() => new Promise<Response>((done) => { resolve = done; }));
  render(<AdapterUploadControl send={send} onUploaded={vi.fn()} />);

  pick("pending.safetensors");
  expect(screen.getByText("Uploading pending.safetensors...")).toBeTruthy();
  expect(button().disabled).toBe(true);

  await act(async () => { resolve(ok()); });
  expect(screen.getByText(successText)).toBeTruthy();
  expect(button().disabled).toBe(false);
});

it("does not open or send when disabled", () => {
  const send = vi.fn(async () => ok());
  render(<AdapterUploadControl send={send} onUploaded={vi.fn()} disabled />);
  expect(button().disabled).toBe(true);
  expect(fileInput().disabled).toBe(true);
  expect(send).not.toHaveBeenCalled();
});

it("clears the previous error immediately when a second file is chosen", async () => {
  let resolve!: (value: Response) => void;
  const send = vi.fn(async () => refused());
  const onUploaded = vi.fn();
  render(<AdapterUploadControl send={send} onUploaded={onUploaded} />);
  pick("first.safetensors");
  await screen.findByRole("alert");

  send.mockImplementationOnce(() => new Promise<Response>((done) => { resolve = done; }));
  pick("second.safetensors");
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByText("Uploading second.safetensors...")).toBeTruthy();

  await act(async () => { resolve(ok()); });
  expect(screen.getByText(successText)).toBeTruthy();
  expect(onUploaded).toHaveBeenCalledTimes(1);
});

it("ignores a file input change with no file", () => {
  const send = vi.fn(async () => ok());
  render(<AdapterUploadControl send={send} onUploaded={vi.fn()} />);
  fireEvent.change(fileInput(), { target: { files: [] } });
  expect(send).not.toHaveBeenCalled();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(button().disabled).toBe(false);
});
