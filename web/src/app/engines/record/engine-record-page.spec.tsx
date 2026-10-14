import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { enginesApi, SECRET_MASK, type EngineView } from "@/lib/engines-api";
import { check, definitions, engineView, renderWithClient, testResult } from "../__fixtures__/test-utils";
import { clockTime } from "../add-engine/draft";
import { EngineRecordPage } from "./engine-record-page";

const viewer = vi.hoisted(() => ({
  current: { authenticated: true, name: "Tim", email: null, role: "owner", admin: true },
}));
vi.mock("@/components/shell/session", () => ({ useViewer: () => viewer.current }));

const registry = { schema_version: 1, services: {} } as never;
let view: EngineView;

function mount(over: Partial<EngineView> = {}) {
  view = engineView(over);
  vi.spyOn(enginesApi, "view").mockImplementation(async () => structuredClone(view));
  return renderWithClient(
    <EngineRecordPage serviceId={view.service_id} onBack={() => undefined} onRemove={() => undefined} />,
  );
}

beforeEach(() => {
  viewer.current = { authenticated: true, name: "Tim", email: null, role: "owner", admin: true };
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(structuredClone(definitions));
});
afterEach(() => cleanup());

describe("i: the engine record page", () => {
  it("draws the sections, the jump list, the state and the saved key", async () => {
    mount();
    expect(await screen.findByRole("heading", { level: 1, name: "garage-gpu" })).toBeVisible();
    const jump = screen.getByRole("navigation", { name: "On this page" });
    expect(within(jump).getAllByRole("link").map((a) => a.textContent)).toEqual([
      "Connection",
      "What it can do",
      "Models",
      "More options",
    ]);
    expect(screen.getByText("Ready")).toBeVisible();
    expect(screen.getByText(/^Last checked/)).toBeVisible();
    expect(screen.getByRole("switch", { name: "Enabled" })).toBeChecked();
    expect(screen.getByText("Configured")).toBeVisible();
    expect(screen.getByRole("button", { name: "Replace key" })).toBeVisible();
    expect(screen.getByTestId("check-line")).toHaveTextContent(/^Reached the server\. Key accepted\. 2 models\. Checked/);
    expect(screen.getAllByTestId("call-line")[0]).toHaveTextContent("Azizam will call GET http://192.168.1.50:8080/v1/models");
    expect(screen.getByRole("checkbox", { name: "Chat" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Images" })).not.toBeChecked();
    expect(screen.getByText("2 of 2 models offered")).toBeVisible();
    expect(screen.queryByText(/Advanced JSON/)).toBeNull();
    expect(screen.queryByText("See it in Activity")).toBeNull();
  });

  it("keeps the stored record read-only and lists other settings", async () => {
    mount();
    const pre = await screen.findByLabelText("Stored record, read only");
    expect(pre.tagName).toBe("PRE");
    expect(pre).toHaveTextContent('"api_key_secret_id": "configured"');
    expect(screen.getByText("Azizam kept these values, but this form has no field for them. They are not lost.")).toBeVisible();
    expect(screen.getByText("probe_interval_s")).toBeVisible();
    expect(screen.getByText("bought 2025")).toBeVisible();
    expect(screen.getByRole("button", { name: "Copy the stored record" })).toBeVisible();
    for (const box of document.querySelectorAll("textarea")) expect(box).not.toHaveValue(expect.stringContaining("{"));
  });

  it("runs a record-page test with the saved settings and stores a pass", async () => {
    const user = userEvent.setup();
    mount();
    const result = { ...testResult("chat_pass"), via: "saved" as const };
    const test = vi.spyOn(enginesApi, "test").mockResolvedValue(result);
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    await user.click(await screen.findByRole("button", { name: "Run a real test for Chat" }));
    expect(await screen.findByText(`Test passed at ${clockTime(result.at)}.`)).toBeVisible();
    expect(screen.getByText("Ran on the gateway’s computer with the saved settings. It is not in Activity.")).toBeVisible();
    expect(test.mock.calls[0][0]).toMatchObject({ service_id: "garage-gpu", capability: "chat" });
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith("garage-gpu", {
        definition: "openai-compatible",
        tests: { chat: { ok: true, at: result.at, seconds: result.seconds } },
      }),
    );
  });
});

describe("N2: editing the address", () => {
  it("withholds the key, blocks Save until a check passes, and says Save removes the key", async () => {
    const user = userEvent.setup();
    mount();
    const address = await screen.findByLabelText("Address");
    await user.clear(address);
    await user.type(address, "http://192.168.1.51:8080/v1");
    expect(screen.getByText("Your saved key won’t be sent to a new address. Re-enter it, or keep the old address.")).toBeVisible();
    expect(screen.getByPlaceholderText("Paste the key again")).toHaveAttribute("type", "password");
    expect(screen.getByText("Key not sent to this address yet. Re-enter it to check.")).toBeVisible();
    expect(screen.queryByText("Configured")).toBeNull();
    expect(screen.queryByText(/Key accepted/)).toBeNull();
    expect(screen.getByTestId("check-line")).toHaveTextContent("Out of date. Check again.");
    expect(screen.getByTestId("check-line")).toHaveTextContent(/Last result, for the old address: reached the server\. 2 models\./);
    expect(screen.getByText(/Saving removes the old key from this engine\./)).toBeVisible();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save and keep off" })).toBeVisible();

    const run = vi.spyOn(enginesApi, "check").mockResolvedValue({ ...check("oai_ok"), key_withheld: true });
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText(/Reached the server at the new address\. No key was sent\./)).toBeVisible();
    expect(run.mock.calls[0][0]).toMatchObject({ service_id: "garage-gpu", values: { address: "http://192.168.1.51:8080/v1" } });
    expect(run.mock.calls[0][0].values).not.toHaveProperty("api_key");
    expect(screen.queryByText(/Key accepted/)).toBeNull();
    const update = vi.spyOn(enginesApi, "update").mockImplementation(async () => {
      view = { ...view, values: { ...view.values, address: "http://192.168.1.51:8080/v1" }, key: { state: "none" } };
      return registry;
    });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1].values).toMatchObject({ address: "http://192.168.1.51:8080/v1" });
    expect(update.mock.calls[0][1].values).not.toHaveProperty("api_key");
    expect(await screen.findByText("No key")).toBeVisible();
    expect(screen.queryByText("Configured")).toBeNull();
  });

  it("a re-entered key that passes the check may say Key accepted", async () => {
    const user = userEvent.setup();
    mount();
    const address = await screen.findByLabelText("Address");
    await user.clear(address);
    await user.type(address, "http://192.168.1.51:8080/v1");
    await user.type(screen.getByPlaceholderText("Paste the key again"), "sk-new");
    const run = vi.spyOn(enginesApi, "check").mockResolvedValue(check("oai_ok_key"));
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText(/Reached the server at the new address\. Key accepted\./)).toBeVisible();
    expect(run.mock.calls[0][0].values).toMatchObject({ api_key: "sk-new" });
    expect(screen.queryByText(/Saving removes the old key/)).toBeNull();
  });

  it("a new path on the same server keeps the key: it is still sent, so no N2 warning", async () => {
    const user = userEvent.setup();
    mount();
    const address = await screen.findByLabelText("Address");
    await user.clear(address);
    await user.type(address, "http://192.168.1.50:8080/api/v1");
    expect(screen.queryByText(/Your saved key won’t be sent/)).toBeNull();
    expect(screen.queryByText(/Saving removes the old key/)).toBeNull();
    expect(screen.getByText("Configured")).toBeVisible();
    expect(screen.getByTestId("check-line")).toHaveTextContent("Out of date. Check again.");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("Keep the old address undoes the edit", async () => {
    const user = userEvent.setup();
    mount();
    const address = await screen.findByLabelText("Address");
    await user.clear(address);
    await user.type(address, "http://10.0.0.9:8080/v1");
    await user.click(screen.getByRole("button", { name: "Keep the old address" }));
    expect(address).toHaveValue("http://192.168.1.50:8080/v1");
    expect(screen.getByText("Configured")).toBeVisible();
    expect(screen.queryByText("You have unsaved changes.")).toBeNull();
  });

  it("an administrator is told only the owner can enter the key again", async () => {
    viewer.current = { ...viewer.current, role: "admin" };
    const user = userEvent.setup();
    mount();
    const address = await screen.findByLabelText("Address");
    await user.clear(address);
    await user.type(address, "http://192.168.1.51:8080/v1");
    expect(
      screen.getByText("Your saved key won’t be sent to a new address. Only the owner can enter it again. Keep the old address, or ask the owner."),
    ).toBeVisible();
    expect(screen.getByText("Not sent to the new address")).toBeVisible();
    expect(screen.queryByPlaceholderText("Paste the key again")).toBeNull();
  });
});

describe("kept-off engines", () => {
  it("l: not checked yet; turning it on runs the check first", async () => {
    const user = userEvent.setup();
    mount({ enabled: false, check: { status: "not_started" }, key: { state: "none" }, capabilities: [], models: [] });
    expect(await screen.findByText("Not checked yet; checked when you turn it on or press Check now")).toBeVisible();
    expect(screen.getAllByText("Not checked yet").length).toBeGreaterThan(2);
    expect(screen.getByText("Not known yet. Azizam looks the first time the engine starts, or when you press Check now.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Open Start and stop" })).toBeVisible();
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    await user.click(screen.getByRole("switch", { name: "Enabled" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("garage-gpu", { definition: "openai-compatible", enabled: true, recheck: true }));
  });

  it("l2: waiting for the owner's key; an administrator cannot turn it on", async () => {
    viewer.current = { ...viewer.current, role: "admin" };
    mount({ enabled: false, check: { status: "not_started", reason: "needkey" }, key: { state: "none" }, capabilities: [], models: [] });
    expect(await screen.findByText("Waiting for the owner to add a key")).toBeVisible();
    expect(screen.getByRole("switch", { name: "Enabled" })).toBeDisabled();
    expect(screen.getByText("No key yet")).toBeVisible();
    expect(screen.getByText("The server needs one. Only the owner can add it.")).toBeVisible();
  });
});

describe("locked and other kinds", () => {
  it("a locked engine changes only its name and Enabled", async () => {
    mount({ locked: true });
    expect(await screen.findByText("Azizam runs this engine itself. Only its name and Enabled can change.")).toBeVisible();
    expect(screen.getByLabelText("Address")).toBeDisabled();
    expect(screen.getByLabelText("Name")).toBeEnabled();
  });

  it("a speech engine shows its path and its model fields; a workflow engine shows N1", async () => {
    mount({
      definition: "speech-to-text-custom-path",
      values: { ...engineView().values, address: "http://192.168.1.60:8178", path: "/inference", public_name: "whisper", server_model: "" },
      capabilities: ["stt"],
      models: [],
      key: { state: "none" },
    });
    expect(await screen.findByLabelText("Path on the server")).toHaveValue("/inference");
    expect(screen.getByTestId("call-line")).toHaveTextContent("Azizam will call POST http://192.168.1.60:8178/inference");
    expect(screen.getByLabelText(/^Public name/)).toHaveValue("whisper");
    expect(within(screen.getByRole("navigation", { name: "On this page" })).getByRole("link", { name: "Model" })).toBeVisible();
    cleanup();
    mount({
      definition: "node-graph",
      values: { ...engineView().values, address: "http://192.168.1.50:8188", workflow_public_name: "sdxl" },
      capabilities: ["graph_images"],
      models: [],
      key: { state: "none" },
      workflow: { public_name: "sdxl", file: "sdxl-text-to-image.json", makes: "images", inputs: { prompt: "6.text" }, node_count: 7 },
    });
    expect(await screen.findByText("sdxl-text-to-image.json")).toBeVisible();
    expect(screen.getByText("7 nodes. Makes images.")).toBeVisible();
    expect(screen.getByLabelText(/^Name apps use/)).toHaveValue("sdxl");
    expect(screen.getByText("Which field gets which input")).toBeVisible();
  });
});

describe("headers and the stored key value", () => {
  it("a stored secret header is write-only: Configured with Replace for the owner, locked for an admin", async () => {
    const user = userEvent.setup();
    const headers = [
      { name: "X-Client", value: "azizam-home", secret: false },
      { name: "X-Client-Token", value: SECRET_MASK, secret: true },
    ];
    mount({ values: { ...engineView().values, headers } });
    const more = await screen.findByText("More options", { selector: "summary span" });
    expect(more.closest("details")).toHaveAttribute("open");
    const values = screen.getAllByLabelText("Value");
    expect(values[1]).toHaveValue("Configured");
    expect(values[1]).toBeDisabled();
    expect(screen.queryByDisplayValue(SECRET_MASK)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Replace X-Client-Token" }));
    expect(screen.getAllByLabelText("Value")[1]).toHaveAttribute("type", "password");
    cleanup();
    viewer.current = { ...viewer.current, role: "admin" };
    mount({ values: { ...engineView().values, headers } });
    await screen.findByText("More options", { selector: "summary span" });
    expect(screen.queryByRole("button", { name: "Replace X-Client-Token" })).toBeNull();
    expect(screen.getByText("Only the owner can add or replace secret values.")).toBeVisible();
  });

  it("saving an untouched page sends the stored headers and adjustments back unchanged", async () => {
    const user = userEvent.setup();
    const transforms = [
      "reasoning-effort-final-chat",
      { kind: "value-map" as const, param: "reasoning_effort", map: { xhigh: "high" }, when: { model: "x" } },
      { kind: "param-map" as const, from: "a", to: "b" },
      { kind: "param-map" as const, from: "c", to: "d" },
    ];
    const headers = [{ name: "X-Client-Token", value: SECRET_MASK, secret: true }];
    mount({ values: { ...engineView().values, headers, transforms } });
    const name = await screen.findByLabelText("Name");
    await user.type(name, "!");
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1]).toMatchObject({ name: "garage-gpu!", values: { headers, transforms } });
  });
});
