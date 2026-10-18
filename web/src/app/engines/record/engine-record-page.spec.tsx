import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "@/lib/api";
import { enginesApi, type EngineView } from "@/lib/engines-api";
import { accessAnswer, check, definitions, engineView, renderWithClient, testResult } from "../__fixtures__/test-utils";
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
  vi.spyOn(enginesApi, "access").mockResolvedValue(accessAnswer("nobody"));
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
    expect(screen.getAllByRole("checkbox", { name: /^(Chat|Embeddings|Images|Reranking|Speech to text|Text to speech|Live voice)$/ })).toHaveLength(2);
    expect(screen.queryByRole("checkbox", { name: "Images" })).toBeNull();
    expect(screen.getByText("2 of 2 models offered")).toBeVisible();
    expect(screen.queryByText(/Advanced JSON/)).toBeNull();
    expect(screen.queryByText("See it in Activity")).toBeNull();
  });

  it("keeps the stored record read-only and lists other settings", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByText("Technical details", { selector: "summary" }));
    const pre = await screen.findByLabelText("Stored record, read only");
    expect(pre.tagName).toBe("PRE");
    expect(pre).toHaveTextContent('"api_key_secret_id": "configured"');
    expect(screen.getByText("Azizam kept these values, but this form has no field for them. They are not lost.")).toBeVisible();
    expect(screen.getByText("probe_interval_s")).toBeVisible();
    expect(screen.getByText("bought 2025")).toBeVisible();
    expect(screen.getByText(/2 settings are kept but not shown/)).toBeVisible();
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

describe("who can use the models", () => {
  it("shows the saved answer under the models list, and does not ask the route while the list is unchanged", async () => {
    mount();
    const heading = await screen.findByRole("heading", { name: "Who can use these models" });
    const models = screen.getByRole("heading", { level: 2, name: "Models" }).closest("section") as HTMLElement;
    expect(models).toContainElement(heading);
    expect(within(models).getByText(view.access.sentence)).toBeVisible();
    expect(within(models).getByRole("link", { name: "Family" })).toHaveAttribute("href", expect.stringMatching(/^\/people\/?\?plan=plan_family$/));
    expect(within(models).getByRole("link", { name: "Access plans" })).toBeVisible();
    expect(within(models).queryByRole("checkbox", { name: /who can use/i })).toBeNull();
    expect(within(models).queryByText("After you save")).toBeNull();
    expect(enginesApi.access).not.toHaveBeenCalled();
  });

  it("editing a public name asks the route with the new name, and labels the answer as after you save", async () => {
    const user = userEvent.setup();
    mount();
    const names = await screen.findAllByLabelText("Public name");
    await user.clear(names[0]);
    await user.type(names[0], "my-chat");
    await waitFor(() => expect(enginesApi.access).toHaveBeenCalled(), { timeout: 3000 });
    await waitFor(() =>
      expect(enginesApi.access).toHaveBeenLastCalledWith({ capabilities: ["chat", "embeddings"], models: ["my-chat", "nomic-embed-text"] }),
    );
    expect(await screen.findByText("After you save")).toBeVisible();
    expect(await screen.findByText(/^No access plan includes these yet/)).toBeVisible();
    expect(screen.getAllByRole("heading", { name: "Who can use these models" })).toHaveLength(1);
  });

  it("turning a model off leaves its name out of the question; turning it back on restores the saved answer", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("checkbox", { name: "Offer nomic-embed-text-v1.5" }));
    await waitFor(() =>
      expect(enginesApi.access).toHaveBeenLastCalledWith({ capabilities: ["chat", "embeddings"], models: ["qwen3-32b"] }),
    );
    await user.click(screen.getByRole("checkbox", { name: "Offer nomic-embed-text-v1.5" }));
    await waitFor(() => expect(screen.queryByText("After you save")).toBeNull());
    expect(screen.getByText(view.access.sentence)).toBeVisible();
  });

  it("after Save, the reloaded view's answer is shown", async () => {
    const user = userEvent.setup();
    mount();
    const names = await screen.findAllByLabelText("Public name");
    await user.clear(names[0]);
    await user.type(names[0], "my-chat");
    const update = vi.spyOn(enginesApi, "update").mockImplementation(async () => {
      view = {
        ...view,
        models: view.models.map((m, i) => (i === 0 ? { ...m, public_name: "my-chat" } : m)),
        access: accessAnswer("listed_only"),
      };
      return registry;
    });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(await screen.findByText(accessAnswer("listed_only").sentence)).toBeVisible();
    expect(screen.queryByText("After you save")).toBeNull();
  });

  it("says when the route fails and offers Try again, without blocking Save", async () => {
    const user = userEvent.setup();
    mount();
    const names = await screen.findAllByLabelText("Public name");
    await user.clear(names[0]);
    // The first question goes out as soon as the list changes; the one after the last keystroke fails.
    await waitFor(() => expect(enginesApi.access).toHaveBeenCalledTimes(1));
    vi.mocked(enginesApi.access).mockRejectedValueOnce(new Error("boom"));
    await user.type(names[0], "my-chat");
    expect(await screen.findByText(/Azizam couldn’t read the access plans\./, undefined, { timeout: 3000 })).toBeVisible();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/^No access plan includes these yet/)).toBeVisible();
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
    expect(screen.getAllByText("Not checked yet")).toHaveLength(2);
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

describe("an engine the router does not call at its own address", () => {
  const reason = "The model router sends this engine’s models somewhere else, so changing its address here would do nothing.";

  it("shows the settings read-only with the reason, and no inputs for them", async () => {
    mount({ editable: false, not_editable_reason: reason });
    expect(await screen.findByText(reason)).toBeVisible();
    expect(screen.queryByLabelText("Address")).toBeNull();
    expect(screen.getByText("http://192.168.1.50:8080/v1")).toBeVisible();
    expect(screen.queryByRole("button", { name: /Replace key|Add a key|Check again|Check now|Detect models again/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Run a real test/ })).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByLabelText("Public name")).toBeNull();
    expect(screen.queryByText("More options", { selector: "summary span" })).toBeNull();
    expect(screen.getByLabelText("Name")).toBeEnabled();
    expect(screen.getByRole("switch", { name: "Enabled" })).toBeEnabled();
    // The models, their state, and who can use them are still shown.
    expect(screen.getByText("qwen3-32b-instruct-q4_k_m")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Who can use these models" })).toBeVisible();
    expect(screen.getByText(view.access.sentence)).toBeVisible();
    expect(enginesApi.access).not.toHaveBeenCalled();
  });

  it("renames with a plain edit that names no definition and no values", async () => {
    const user = userEvent.setup();
    mount({ editable: false, not_editable_reason: reason });
    const edit = vi.spyOn(api.services, "edit").mockImplementation(async () => {
      view = { ...view, name: "garage" };
      return registry;
    });
    const update = vi.spyOn(enginesApi, "update");
    const name = await screen.findByLabelText("Name");
    await user.clear(name);
    await user.type(name, "garage");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(edit).toHaveBeenCalledWith("garage-gpu", { name: "garage" }));
    expect(edit.mock.calls[0][1]).not.toHaveProperty("definition");
    expect(edit.mock.calls[0][1]).not.toHaveProperty("values");
    expect(update).not.toHaveBeenCalled();
    expect(await screen.findByRole("heading", { level: 1, name: "garage" })).toBeVisible();
  });

  it("turns on and off with a plain edit", async () => {
    const user = userEvent.setup();
    mount({ editable: false, not_editable_reason: reason });
    const edit = vi.spyOn(api.services, "edit").mockResolvedValue(registry);
    await user.click(await screen.findByRole("switch", { name: "Enabled" }));
    await waitFor(() => expect(edit).toHaveBeenCalledWith("garage-gpu", { enabled: false }));
    expect(edit.mock.calls[0][1]).not.toHaveProperty("definition");
  });
});

describe("headers", () => {
  it("are plain text rows with no secret toggle, for the owner and for an administrator", async () => {
    const headers = [
      { name: "X-Client", value: "azizam-home" },
      { name: "X-Region", value: "lan" },
    ];
    mount({ values: { ...engineView().values, headers } });
    const more = await screen.findByText("More options", { selector: "summary span" });
    expect(more.closest("details")).toHaveAttribute("open");
    expect(screen.getAllByLabelText("Value").map((v) => (v as HTMLInputElement).value)).toEqual(["azizam-home", "lan"]);
    for (const value of screen.getAllByLabelText("Value")) expect(value).not.toHaveAttribute("type", "password");
    expect(screen.queryByRole("checkbox", { name: "Secret" })).toBeNull();
    expect(screen.queryByText("Secret")).toBeNull();
    expect(screen.queryByText(/secret values/i)).toBeNull();
    expect(screen.getByText(/They are saved as plain text, so don’t put a key here\./)).toBeVisible();
    cleanup();
    viewer.current = { ...viewer.current, role: "admin" };
    mount({ values: { ...engineView().values, headers } });
    await screen.findByText("More options", { selector: "summary span" });
    for (const value of screen.getAllByLabelText("Value")) expect(value).toBeEnabled();
    expect(screen.queryByRole("button", { name: /^Replace X-/ })).toBeNull();
  });

  it("saving an untouched page sends the headers and adjustments back unchanged; a new header has no secret flag", async () => {
    const user = userEvent.setup();
    const transforms = [
      "reasoning-effort-final-chat",
      { kind: "value-map" as const, param: "reasoning_effort", map: { xhigh: "high" }, when: { model: "x" } },
      { kind: "param-map" as const, from: "a", to: "b" },
      { kind: "param-map" as const, from: "c", to: "d" },
    ];
    const headers = [{ name: "X-Client", value: "azizam-home" }];
    mount({ values: { ...engineView().values, headers, transforms } });
    const name = await screen.findByLabelText("Name");
    await user.type(name, "!");
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1]).toMatchObject({ name: "garage-gpu!", values: { headers, transforms } });
    update.mockClear();
    await user.click(screen.getByRole("button", { name: "Add a header" }));
    const names = screen.getAllByLabelText("Header name");
    await user.type(names[1], "X-Region");
    await user.type(screen.getAllByLabelText("Value")[1], "lan");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1].values?.headers).toEqual([
      { name: "X-Client", value: "azizam-home" },
      { name: "X-Region", value: "lan" },
    ]);
  });
});

describe("a locked engine", () => {
  it("changes only its name and Enabled", async () => {
    mount({ locked: true });
    expect(await screen.findByText("Azizam runs this engine itself. Only its name and Enabled can change.")).toBeVisible();
    expect(screen.getByLabelText("Address")).toBeDisabled();
    expect(screen.getByLabelText("Name")).toBeEnabled();
  });
});
