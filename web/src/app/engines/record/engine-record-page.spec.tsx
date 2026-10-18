import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
      "What it offers",
      "How it runs",
      "What Azizam can check",
      "Resources",
      "Stored record",
    ]);
    expect(screen.getByText("On")).toBeVisible();
    expect(screen.getByText(/^Last checked/)).toBeVisible();
    expect(screen.getByRole("switch", { name: "Enabled" })).toBeChecked();
    expect(within(screen.getByRole("group", { name: "Saved key" })).getByText("Configured")).toBeVisible();
    expect(screen.getByLabelText("API key")).toHaveValue("");
    expect(screen.getByLabelText("API key")).toHaveAttribute("type", "password");
    expect(screen.queryByRole("button", { name: "Replace key" })).toBeNull();
    expect(screen.getByTestId("check-line")).toHaveTextContent(/^Reached the server\. A key was sent\. Acceptance was not recorded\. 2 models found\. Checked/);
    expect(screen.getAllByTestId("call-line")[0]).toHaveTextContent("Connection check: GET http://192.168.1.50:8080/v1/models");
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
    await user.click(await screen.findByRole("heading", { name: "Stored record" }));
    const pre = await screen.findByLabelText("Stored record, read only");
    expect(pre.tagName).toBe("PRE");
    expect(pre).toHaveTextContent('"api_key_secret_id": "configured"');
    expect(screen.getByText("These saved settings have no editor on this page.")).toBeVisible();
    expect(screen.getByText("probe_interval_s")).toBeVisible();
    expect(screen.getByText("bought 2025")).toBeVisible();
    expect(screen.getByRole("button", { name: "Copy the stored record" })).toBeVisible();
    for (const box of document.querySelectorAll("textarea")) expect(box).not.toHaveValue(expect.stringContaining("{"));
  });

  it("runs a record-page test with the saved settings and stores a pass", async () => {
    const user = userEvent.setup();
    mount();
    const result = { ...testResult("chat_pass"), via: "saved" as const, evidence_token: "server-issued" };
    const test = vi.spyOn(enginesApi, "test").mockResolvedValue(result);
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    await user.click(await screen.findByRole("button", { name: "Run a real test for Chat" }));
    expect(await screen.findByText(`Test passed at ${clockTime(result.at)}.`)).toBeVisible();
    expect(screen.getByText("Tested the saved settings from the gateway’s computer. This test is not listed in Activity.")).toBeVisible();
    expect(test.mock.calls[0][0]).toMatchObject({ service_id: "garage-gpu", capability: "chat" });
    await waitFor(() =>
      expect(update).toHaveBeenCalledWith("garage-gpu", {
        definition: "openai-compatible",
        tests: { chat: { ok: true, at: result.at, seconds: result.seconds, evidence_token: "server-issued" } },
      }),
    );
  });
});

describe("who can use the models", () => {
  it("shows the saved answer under the models list, and does not ask the route while the list is unchanged", async () => {
    mount();
    const heading = await screen.findByRole("heading", { name: "Who can use these models" });
    const models = screen.getByRole("heading", { level: 2, name: "What it offers" }).closest("section") as HTMLElement;
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
    const names = await screen.findAllByLabelText("Name apps use");
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
    const names = await screen.findAllByLabelText("Name apps use");
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
    const names = await screen.findAllByLabelText("Name apps use");
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
  it("withholds the old key, disables secret edits and requires a check or an explicit keep-off save", async () => {
    const user = userEvent.setup();
    mount();
    const address = await screen.findByLabelText("Address");
    await user.clear(address);
    await user.type(address, "http://192.168.1.51:8080/v1");
    expect(screen.getByText("Your saved key won’t be sent to a new address. Re-enter it, or keep the old address.")).toBeVisible();
    expect(screen.getByLabelText("API key")).toHaveValue("");
    expect(screen.getByLabelText("API key")).toBeDisabled();
    expect(screen.getByText(/Save the address before editing secrets/)).toBeVisible();
    expect(screen.getByTestId("check-line")).toHaveTextContent("Out of date. Check again.");
    expect(screen.getByTestId("check-line")).toHaveTextContent(/Last result, for the old address: reached the server\. 2 models found\./);
    expect(screen.getByText(/Saving removes the old key from this engine\./)).toBeVisible();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save and keep off" })).toBeVisible();

    const run = vi.spyOn(enginesApi, "check").mockResolvedValue({ ...check("oai_ok"), key_sent: false, key_withheld: true });
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText(/Reached the server at the new address\. No key was sent for this check\./)).toBeVisible();
    expect(run.mock.calls[0][0]).toMatchObject({ service_id: "garage-gpu", values: { address: "http://192.168.1.51:8080/v1" } });
    expect(run.mock.calls[0][0].values).not.toHaveProperty("api_key");
    expect(screen.queryByText(/A key was sent and accepted/)).toBeNull();
    const update = vi.spyOn(enginesApi, "update").mockImplementation(async () => {
      view = { ...view, values: { ...view.values, address: "http://192.168.1.51:8080/v1" }, key: { state: "none" } };
      return registry;
    });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1].values).toMatchObject({ address: "http://192.168.1.51:8080/v1" });
    expect(update.mock.calls[0][1].values).not.toHaveProperty("api_key");
    expect(await within(screen.getByRole("group", { name: "Saved key" })).findByText("No key configured")).toBeVisible();
  });

  it("saves the new address kept off before adding its key, then checks with the saved secret", async () => {
    const user = userEvent.setup();
    mount({ revision: '"os-7"' });
    const address = await screen.findByLabelText("Address");
    await user.clear(address);
    await user.type(address, "http://192.168.1.51:8080/v1");
    const update = vi.spyOn(enginesApi, "update").mockImplementation(async () => {
      view = { ...view, enabled: false, revision: '"os-8"', check: { status: "not_started" }, values: { ...view.values, address: "http://192.168.1.51:8080/v1" }, key: { state: "none" } };
      return registry;
    });
    await user.click(screen.getByRole("button", { name: "Save and keep off" }));
    await waitFor(() => expect(screen.getByLabelText("API key")).toBeEnabled());
    expect(update.mock.calls[0][1]).toMatchObject({ disabled: true, values: { address: "http://192.168.1.51:8080/v1" } });
    expect(update.mock.calls[0][1].values).not.toHaveProperty("api_key");
    const commands = vi.spyOn(enginesApi, "secretEntries").mockImplementation(async () => {
      view = { ...view, revision: '"os-9"', key: { state: "configured", source: "stored", entry_id: "ent_new_origin", origin: "http://192.168.1.51:8080" } };
      return registry;
    });
    const run = vi.spyOn(enginesApi, "check").mockResolvedValue(check("oai_ok_key"));
    await user.type(screen.getByLabelText("API key"), "new-origin-key");
    expect(screen.getByLabelText("Address")).toBeDisabled();
    await user.click(screen.getByRole("button", { name: /Check (again|now)/ }));
    expect(await screen.findByText(/Save the secret changes before checking this engine/)).toBeVisible();
    expect(run).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(commands).toHaveBeenCalledWith("garage-gpu", [{ carrier: "config.api_key", operation: "add", value: "new-origin-key" }], '"os-8"'));
    await waitFor(() => expect(screen.getByLabelText("API key")).toHaveValue(""));
    expect(update).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: /Check (again|now)/ }));
    expect(await screen.findByText(/Reached the server\. A key was sent and accepted\./)).toBeVisible();
    expect(run.mock.calls[0][0]).toMatchObject({ service_id: "garage-gpu", values: { address: "http://192.168.1.51:8080/v1" } });
    expect(run.mock.calls[0][0].values).not.toHaveProperty("api_key");
    expect(JSON.stringify(commands.mock.calls)).not.toContain("[redacted]");
  });

  it("a new path on the same server keeps the key binding while requiring a fresh check", async () => {
    const user = userEvent.setup();
    mount();
    const address = await screen.findByLabelText("Address");
    await user.clear(address);
    await user.type(address, "http://192.168.1.50:8080/api/v1");
    expect(screen.queryByText(/Your saved key won’t be sent/)).toBeNull();
    expect(screen.queryByText(/Saving removes the old key/)).toBeNull();
    expect(within(screen.getByRole("group", { name: "Saved key" })).getByText("Configured")).toBeVisible();
    expect(screen.getByLabelText("API key")).toBeDisabled();
    expect(screen.getByTestId("check-line")).toHaveTextContent("Out of date. Check again.");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("Keep the old address undoes the edit and restores an empty editable key input", async () => {
    const user = userEvent.setup();
    mount();
    const address = await screen.findByLabelText("Address");
    await user.clear(address);
    await user.type(address, "http://10.0.0.9:8080/v1");
    await user.click(screen.getByRole("button", { name: "Keep the old address" }));
    expect(address).toHaveValue("http://192.168.1.50:8080/v1");
    expect(within(screen.getByRole("group", { name: "Saved key" })).getByText("Configured")).toBeVisible();
    expect(screen.getByLabelText("API key")).toBeEnabled();
    expect(screen.getByLabelText("API key")).toHaveValue("");
    expect(screen.queryByText("You have unsaved changes.")).toBeNull();
  });

  it("an administrator cannot change the address or secret entry", async () => {
    viewer.current = { ...viewer.current, role: "admin" };
    mount();
    expect(await screen.findByLabelText("Address")).toBeDisabled();
    expect(screen.getByLabelText("API key")).toBeDisabled();
    expect(screen.getByLabelText("API key")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Clear API key" })).toBeDisabled();
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
    expect(within(screen.getByRole("group", { name: "Saved key" })).getByText("No key configured")).toBeVisible();
    expect(screen.getByLabelText("API key")).toBeDisabled();
  });
});

describe("inference key commands", () => {
  it("replaces a configured key by stable id and keeps the ordinary record free of key input", async () => {
    const user = userEvent.setup();
    mount({ revision: '"os-12"' });
    const input = await screen.findByLabelText("API key");
    expect(input).toHaveValue("");
    const commands = vi.spyOn(enginesApi, "secretEntries").mockResolvedValue(registry);
    const update = vi.spyOn(enginesApi, "update");
    const test = vi.spyOn(enginesApi, "test");
    await user.type(input, "replacement-key");
    expect(screen.getByLabelText("Address")).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Run a real test for Chat" }));
    expect(await screen.findByText(/Save the secret changes before running a test/)).toBeVisible();
    expect(test).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(commands).toHaveBeenCalledWith("garage-gpu", [
      { carrier: "config.api_key", operation: "replace", entry_id: "ent_fixture_key", value: "replacement-key" },
    ], '"os-12"'));
    expect(update).not.toHaveBeenCalled();
    expect(JSON.stringify(commands.mock.calls)).not.toContain("[redacted]");
    await waitFor(() => expect(screen.getByLabelText("API key")).toHaveValue(""));
  });

  it("clears an environment key binding explicitly by its stable id", async () => {
    const user = userEvent.setup();
    mount({ revision: '"os-20"', key: { state: "configured", source: "env", env_name: "ENGINE_KEY", entry_id: "ent_env_key", origin: "http://192.168.1.50:8080" } });
    const saved = await screen.findByRole("group", { name: "Saved key" });
    expect(saved).toHaveTextContent("Read from ENGINE_KEY");
    expect(screen.getByLabelText("API key")).toHaveValue("");
    const commands = vi.spyOn(enginesApi, "secretEntries").mockImplementation(async () => {
      view = { ...view, key: { state: "none" } };
      return registry;
    });
    const update = vi.spyOn(enginesApi, "update");
    await user.click(screen.getByRole("button", { name: "Clear API key" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(commands).toHaveBeenCalledWith("garage-gpu", [
      { carrier: "config.api_key", operation: "clear", entry_id: "ent_env_key" },
    ], '"os-20"'));
    expect(update).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("group", { name: "Saved key" })).not.toHaveTextContent("ENGINE_KEY"));
    expect(screen.getByRole("group", { name: "Saved key" })).toHaveTextContent("No key configured");
  });
});

describe("headers", () => {
  const headers = [{ id: "ent_client", name: "X-Client", configured: true }, { id: "ent_region", name: "X-Region", configured: true }];
  const record = { ...engineView().record, config: { headers } };

  it("shows configured entries with empty write-only inputs and restricts administrators to reading metadata", async () => {
    mount({ record });
    const group = await screen.findByRole("group", { name: "Request headers" });
    expect(within(group).getAllByText("Configured")).toHaveLength(2);
    for (const header of headers) {
      const input = within(group).getByLabelText(`Value for ${header.name}`);
      expect(input).toHaveValue("");
      expect(input).toHaveAttribute("type", "password");
    }
    expect(within(group).queryByRole("checkbox")).toBeNull();
    expect(within(group).getByText(/Header values are stored as secrets/)).toBeVisible();
    expect(screen.queryByText(/saved as plain text/)).toBeNull();
    cleanup();
    viewer.current = { ...viewer.current, role: "admin" };
    mount({ record });
    const locked = await screen.findByRole("group", { name: "Request headers" });
    for (const header of headers) expect(within(locked).getByLabelText(`Value for ${header.name}`)).toBeDisabled();
  });

  it("a timeout edit omits untouched headers and preserves adjustments; adding a header uses a command", async () => {
    const user = userEvent.setup();
    const transforms = [
      "reasoning-effort-final-chat",
      { kind: "value-map" as const, param: "reasoning_effort", map: { xhigh: "high" }, when: { model: "x" } },
      { kind: "param-map" as const, from: "a", to: "b" },
      { kind: "param-map" as const, from: "c", to: "d" },
    ];
    mount({ record, values: { ...engineView().values, transforms } });
    const name = await screen.findByLabelText("Name");
    await user.type(name, "!");
    const more = screen.getByText("More options", { selector: "summary span" });
    if (!more.closest("details")?.open) await user.click(more);
    await user.clear(screen.getByLabelText("Time to wait for a reply"));
    await user.type(screen.getByLabelText("Time to wait for a reply"), "300");
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    const commands = vi.spyOn(enginesApi, "secretEntries").mockResolvedValue(registry);
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1]).toMatchObject({ name: "garage-gpu!", values: { transforms, reply_timeout_s: 300 } });
    expect(update.mock.calls[0][1].values).not.toHaveProperty("headers");
    expect(commands).not.toHaveBeenCalled();
    update.mockClear();
    await user.click(screen.getByRole("button", { name: "Add a header" }));
    const names = screen.getAllByLabelText("Header name");
    await user.type(names[2], "X-New-Region");
    await user.type(screen.getByLabelText("Value for X-New-Region"), "lan");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(commands).toHaveBeenCalled());
    expect(commands.mock.calls[0][1]).toEqual([{ carrier: "config.headers", operation: "add", name: "X-New-Region", value: "lan" }]);
    expect(update).not.toHaveBeenCalled();
    expect(JSON.stringify(commands.mock.calls)).not.toContain("[redacted]");
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
