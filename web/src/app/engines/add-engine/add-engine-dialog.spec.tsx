import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { enginesApi, type CheckResult } from "@/lib/engines-api";
import { check, definitions, renderWithClient } from "../__fixtures__/test-utils";
import { AddEngineDialog } from "./add-engine-dialog";

const viewer = vi.hoisted(() => ({
  current: { authenticated: true, name: "Tim", email: null, role: "owner", admin: true },
}));
vi.mock("@/components/shell/session", () => ({ useViewer: () => viewer.current }));

const onAdded = vi.fn();
const onOpenEngine = vi.fn();

function mount() {
  return renderWithClient(
    <AddEngineDialog
      open
      onOpenChange={() => undefined}
      onAdded={onAdded}
      onOpenEngine={onOpenEngine}
      checkRevealMs={0}
    />,
  );
}

async function pickKind(user: ReturnType<typeof userEvent.setup>, title: RegExp) {
  await user.click(await screen.findByRole("button", { name: title }));
  return screen.getByRole("dialog", { name: "Add engine" });
}

async function typeAddress(user: ReturnType<typeof userEvent.setup>, address: string) {
  const field = screen.getByLabelText(/^Address/);
  await user.clear(field);
  await user.type(field, address);
}

beforeEach(() => {
  viewer.current = { authenticated: true, name: "Tim", email: null, role: "owner", admin: true };
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(structuredClone(definitions));
});
afterEach(() => {
  cleanup();
  onAdded.mockReset();
  onOpenEngine.mockReset();
});

describe("the dialog frame", () => {
  it("has a title, no missing-description warning, a scrolling body and a pinned footer; Escape closes", async () => {
    const warn = vi.spyOn(console, "warn");
    const error = vi.spyOn(console, "error");
    const onOpenChange = vi.fn();
    const user = userEvent.setup();
    renderWithClient(
      <AddEngineDialog open onOpenChange={onOpenChange} onAdded={onAdded} onOpenEngine={onOpenEngine} checkRevealMs={0} />,
    );
    const dialog = await screen.findByRole("dialog", { name: "Add engine" });
    await within(dialog).findByText("Step 1 of 5 · Kind");
    const body = dialog.querySelector('[data-slot="dialog-body"]');
    const footer = dialog.querySelector('[data-slot="dialog-footer"]');
    expect(dialog).toHaveClass("flex!", "max-h-[85dvh]", "overflow-hidden");
    expect(body).toHaveClass("min-h-0", "flex-1", "overflow-y-auto");
    expect(footer).toHaveClass("shrink-0");
    expect(body).not.toContainElement(footer as HTMLElement);
    expect(within(footer as HTMLElement).getByRole("button", { name: "Cancel" })).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Close" })).toBeVisible();
    const messages = [...warn.mock.calls, ...error.mock.calls].flat().map(String);
    expect(messages.some((m) => m.includes("Missing `Description`"))).toBe(false);
    await user.keyboard("{Escape}");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});

describe("a: kind search", () => {
  it("lists the definitions, first entry first, with badge, example and summary", async () => {
    mount();
    const dialog = await screen.findByRole("dialog", { name: "Add engine" });
    expect(await within(dialog).findByRole("heading", { name: "What kind of server is it?" })).toBeVisible();
    expect(within(dialog).getByText("Step 1 of 5 · Kind")).toBeVisible();
    const kinds = within(within(dialog).getByRole("group", { name: "Server kinds" })).getAllByRole("button");
    expect(kinds).toHaveLength(5);
    expect(kinds[0]).toHaveTextContent("Any OpenAI-compatible server");
    expect(kinds[0]).toHaveTextContent("Most servers");
    expect(kinds[0]).toHaveTextContent("For example, vLLM, llama.cpp or LM Studio");
    expect(kinds[1]).toHaveTextContent("Lists its models at its own address and loads them when asked.");
  });

  it("matches hidden keywords and says what it matched on", async () => {
    const user = userEvent.setup();
    mount();
    await user.type(await screen.findByRole("searchbox", { name: "Search server kinds" }), "ollama");
    const kinds = within(screen.getByRole("group", { name: "Server kinds" })).getAllByRole("button");
    expect(kinds).toHaveLength(1);
    expect(kinds[0]).toHaveTextContent("Server with its own model list");
    expect(kinds[0]).not.toHaveTextContent("Matched on");
    await user.clear(screen.getByRole("searchbox", { name: "Search server kinds" }));
    await user.type(screen.getByRole("searchbox", { name: "Search server kinds" }), "sglang");
    expect(screen.getByRole("button", { name: /Any OpenAI-compatible server/ })).toHaveTextContent("Matched on “sglang”");
  });

  it("a2: says no kind matches and offers the OpenAI-compatible kind", async () => {
    const user = userEvent.setup();
    mount();
    await user.type(await screen.findByRole("searchbox", { name: "Search server kinds" }), "triton");
    expect(screen.getByRole("status")).toHaveTextContent(
      "No kind matches “triton”. If the server speaks the OpenAI style, pick “Any OpenAI-compatible server”.",
    );
    await user.click(screen.getByRole("button", { name: "Use Any OpenAI-compatible server" }));
    expect(screen.getByText("Step 2 of 5 · Address and key")).toBeVisible();
  });
});

describe("b: address and key", () => {
  it("draws the fields from data, the live call line, and the env-var path", async () => {
    const user = userEvent.setup();
    mount();
    const dialog = await pickKind(user, /Any OpenAI-compatible server/);
    expect(within(dialog).getByText("Step 2 of 5 · Address and key")).toBeVisible();
    expect(within(dialog).getByText("Where the server listens, with its port. Usually ends in /v1.")).toBeVisible();
    expect(within(dialog).getByText("Leave empty if the server has no key.")).toBeVisible();
    expect(within(dialog).getByLabelText("API key")).toHaveAttribute("type", "password");
    expect(within(dialog).getByText("Azizam shows the exact request it will make here.")).toBeVisible();
    await typeAddress(user, "http://192.168.1.50:8080/v1/");
    expect(within(dialog).getByTestId("call-line")).toHaveTextContent(
      "Azizam will call GET http://192.168.1.50:8080/v1/models",
    );
    await user.click(within(dialog).getByRole("button", { name: "Read the key from a server environment variable instead" }));
    expect(within(dialog).getByLabelText("Environment variable name")).toHaveAttribute("placeholder", "LAN_SERVER_KEY");
    expect(within(dialog).queryByLabelText("API key")).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Paste the key here instead" }));
    expect(within(dialog).getByLabelText("API key")).toBeVisible();
    expect(within(dialog).getByText(/Continue sends a few harmless requests/)).toBeVisible();
  });

  it("uses the origin for an own-model-list server and the typed path for a custom-path server", async () => {
    const user = userEvent.setup();
    mount();
    let dialog = await pickKind(user, /Server with its own model list/);
    await typeAddress(user, "http://192.168.1.50:11434");
    expect(within(dialog).getByTestId("call-line")).toHaveTextContent("Azizam will call GET http://192.168.1.50:11434/api/tags");
    await user.click(within(dialog).getByRole("button", { name: "Change kind" }));
    dialog = await pickKind(user, /Speech-to-text server with a custom path/);
    expect(within(dialog).getByText("Step 2 of 5 · Address and path")).toBeVisible();
    await typeAddress(user, "http://192.168.1.60:8178");
    expect(within(dialog).getByTestId("call-line")).toHaveTextContent(
      "Azizam will call POST http://192.168.1.60:8178 plus the path you type.",
    );
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/^Path on the server/), "/inference");
    expect(within(dialog).getByTestId("call-line")).toHaveTextContent("Azizam will call POST http://192.168.1.60:8178/inference");
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeEnabled();
  });

  it("says what a valid address looks like before any request", async () => {
    const user = userEvent.setup();
    const run = vi.spyOn(enginesApi, "check");
    mount();
    const dialog = await pickKind(user, /Any OpenAI-compatible server/);
    await typeAddress(user, "192.168.1.50:8080");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(
      within(dialog).getByText(
        "Enter an address that starts with http:// or https://, for example http://192.168.1.50:8080/v1.",
      ),
    ).toBeVisible();
    expect(run).not.toHaveBeenCalled();
  });

  it("b2: an administrator sees the key as owner only, with no env-var link", async () => {
    viewer.current = { ...viewer.current, role: "admin" };
    const user = userEvent.setup();
    mount();
    const dialog = await pickKind(user, /Any OpenAI-compatible server/);
    const key = within(dialog).getByLabelText(/API key/);
    expect(key).toBeDisabled();
    expect(key).toHaveAttribute("placeholder", "Owner only");
    expect(within(dialog).getAllByText("Owner only").length).toBeGreaterThan(0);
    expect(within(dialog).getByText("Only the owner can add a key.")).toBeVisible();
    expect(within(dialog).getByText(/You can still add a server that needs no key\./)).toBeVisible();
    expect(within(dialog).queryByRole("button", { name: /environment variable/ })).toBeNull();
  });

  it("b3: an administrator facing a locked server copies a note and adds it kept off for the owner", async () => {
    viewer.current = { ...viewer.current, role: "admin" };
    const user = userEvent.setup();
    vi.spyOn(enginesApi, "check").mockResolvedValue(check("oai_needkey_admin"));
    const create = vi.spyOn(enginesApi, "create").mockResolvedValue({
      registry: {
        schema_version: 1,
        services: { "engine-at-192-168-1-50-8080": { id: "engine-at-192-168-1-50-8080", name: "Engine at 192.168.1.50:8080" } },
      },
      serviceId: "engine-at-192-168-1-50-8080",
    } as never);
    mount();
    const dialog = await pickKind(user, /Any OpenAI-compatible server/);
    await typeAddress(user, "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(await within(dialog).findByText("This server needs a key. Only the owner can add one.")).toBeVisible();
    expect(within(dialog).getByText(/only the owner can add one\. Save it without a key and ask the owner/)).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Copy a note for the owner" }));
    await expect(navigator.clipboard.readText()).resolves.toBe(
      "Please add the API key for the server at http://192.168.1.50:8080/v1. In Azizam, open Engines, then the engine, then API key.",
    );
    expect(
      within(dialog).getByText(
        "“Add but keep off” saves the engine without a key and turned off. The owner adds the key later, then turns it on.",
      ),
    ).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Add but keep off (owner adds the key)" }));
    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    expect(create.mock.calls[0][0]).toMatchObject({
      definition: "openai-compatible",
      disabled: true,
      keep_off_reason: "needkey",
      values: { address: "http://192.168.1.50:8080/v1" },
    });
    expect(create.mock.calls[0][0].values).not.toHaveProperty("api_key");
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
    expect(onAdded.mock.calls[0][0]).toMatchObject({
      serviceId: "engine-at-192-168-1-50-8080",
      message:
        "Added “Engine at 192.168.1.50:8080”, turned off. The server needs a key, and only the owner can add one. Ask the owner, then turn the engine on.",
    });
  });

  it("b4: a duplicate address offers to open the engine that has it", async () => {
    const user = userEvent.setup();
    vi.spyOn(enginesApi, "check").mockResolvedValue(check("oai_dup"));
    mount();
    const dialog = await pickKind(user, /Any OpenAI-compatible server/);
    await typeAddress(user, "http://192.168.1.20:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(await within(dialog).findByText("Already added")).toBeVisible();
    expect(within(dialog).getByText("An engine already uses this address.")).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Open Kitchen mini PC" }));
    expect(onOpenEngine).toHaveBeenCalledWith("kitchen");
  });
});

describe("c: checking", () => {
  it("shows the four check rows from data and cancels back to the address", async () => {
    const user = userEvent.setup();
    let signal: AbortSignal | undefined;
    vi.spyOn(enginesApi, "check").mockImplementation((_body, s) => {
      signal = s;
      return new Promise<CheckResult>(() => undefined);
    });
    mount();
    const dialog = await pickKind(user, /Any OpenAI-compatible server/);
    await typeAddress(user, "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    const rows = within(await within(dialog).findByRole("status", { name: "Check progress" })).getAllByRole("listitem");
    expect(rows.map((r) => r.textContent?.replace("…", ""))).toEqual([
      "Reaching the server",
      "Checking the key",
      "Listing the models",
      "Finding what it can do",
    ]);
    expect(within(dialog).getByText("Azizam sends only lists and empty requests. Nothing is generated and nothing is saved yet.")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: /Checking/ })).toBeDisabled();
    expect(within(dialog).getByLabelText(/^Address/)).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Cancel check" }));
    expect(signal?.aborted).toBe(true);
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeEnabled();
    expect(within(dialog).getByLabelText(/^Address/)).toHaveValue("http://192.168.1.50:8080/v1");
  });
});

describe("d, e, e2, e3: findings attached to their field", () => {
  async function failWith(name: string, address = "http://192.168.1.50:8080/v1") {
    const user = userEvent.setup();
    const run = vi.spyOn(enginesApi, "check").mockResolvedValue(check(name));
    mount();
    const dialog = await pickKind(user, /Any OpenAI-compatible server/);
    await typeAddress(user, address);
    if (name === "oai_auth") await user.type(within(dialog).getByLabelText("API key"), "sk-bad");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await within(dialog).findByTestId("finding");
    return { user, dialog, run };
  }

  it("d: a refused key shows what, why, fix, details with the curl, and P1 keep-off", async () => {
    const { dialog, run } = await failWith("oai_auth");
    const finding = within(dialog).getByRole("alert", { name: "Key refused" });
    expect(within(finding).getByText("What happened")).toBeVisible();
    expect(within(finding).getByText("The server refused the key.")).toBeVisible();
    expect(within(finding).getByText(/It answered “401 Unauthorized”/)).toBeVisible();
    expect(within(finding).getByText(/Paste the key again\./)).toBeVisible();
    expect(within(dialog).getByText("1 problem. Fix it, or add the engine turned off.")).toBeVisible();
    expect(within(dialog).getByText("Reached the server.")).toBeVisible();
    expect(within(finding).getByText("What the server said")).toBeVisible();
    expect(within(finding).getByText(/curl -i http:\/\/192\.168\.1\.50:8080\/v1\/models/)).toBeVisible();
    expect(within(finding).getByText("Replace YOUR_KEY with your key. Azizam never puts your key in this text.")).toBeVisible();
    expect(run.mock.calls[0][0].values).toMatchObject({ api_key: "sk-bad" });
    expect(within(dialog).getByRole("button", { name: "Add but keep off" })).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Check again" })).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Back" })).toBeVisible();
  });

  it("e: nothing recognised warns, and Add anyway leaves every row unticked with no invented models (P2)", async () => {
    const { user, dialog } = await failWith("oai_nocap");
    expect(within(dialog).getByText("The check finished with 1 warning.")).toBeVisible();
    expect(within(dialog).getByRole("status", { name: "Nothing recognised" })).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Add but keep off" })).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Add anyway" }));
    expect(
      within(dialog).getByText("Azizam could not tell what this server can do. Tick what you know it can do, then run a real test."),
    ).toBeVisible();
    for (const box of within(dialog).getAllByRole("checkbox")) expect(box).not.toBeChecked();
    expect(within(dialog).getByText("Nothing is turned on. The engine will be added but will not serve anything.")).toBeVisible();
    await user.click(within(dialog).getByRole("checkbox", { name: "Chat" }));
    expect(within(dialog).getByText("Azizam could not see this. You can turn it on, but requests may fail.")).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(within(dialog).getByText("0 of 0 models offered")).toBeVisible();
    expect(within(dialog).getByLabelText("Add a model by name")).toBeVisible();
    expect(within(dialog).queryByText("Reported by server")).toBeNull();
    const create = vi.spyOn(enginesApi, "create").mockResolvedValue({ registry: { schema_version: 1, services: {} }, serviceId: "nothing" } as never);
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await user.click(within(dialog).getByRole("button", { name: "Add engine" }));
    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    expect(create.mock.calls[0][0]).toMatchObject({ acknowledge_warnings: true, capabilities: ["chat"], models: [] });
  });

  it("e2: a down server can be added kept off, with the owner's fix text", async () => {
    const create = vi.spyOn(enginesApi, "create").mockResolvedValue({
      registry: {
        schema_version: 1,
        services: { "engine-at-192-168-1-50-8080": { id: "engine-at-192-168-1-50-8080", name: "Engine at 192.168.1.50:8080" } },
      },
      serviceId: "engine-at-192-168-1-50-8080",
    } as never);
    const { user, dialog } = await failWith("oai_down");
    const finding = within(dialog).getByRole("alert", { name: "Server not reachable" });
    expect(within(finding).getByText(/set its start command on the engine page/)).toBeVisible();
    expect(
      within(dialog).getByText(
        "“Add but keep off” adds the engine turned off and skips the check. It shows as not checked yet, and Azizam checks it when you turn it on or press Check now.",
      ),
    ).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Add but keep off" }));
    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    expect(create.mock.calls[0][0]).toMatchObject({ disabled: true, name: "Engine at 192.168.1.50:8080" });
    expect(create.mock.calls[0][0]).not.toHaveProperty("keep_off_reason");
    await waitFor(() =>
      expect(onAdded.mock.calls[0][0].message).toBe(
        "Added “Engine at 192.168.1.50:8080”, turned off. Azizam has not checked the server yet. It checks it when you turn it on or press Check now.",
      ),
    );
  });

  it("e3: a missing /v1 adds it and checks again", async () => {
    const { user, dialog, run } = await failWith("oai_path", "http://192.168.1.50:8080");
    run.mockResolvedValue(check("oai_ok"));
    await user.click(within(dialog).getByRole("button", { name: "Add /v1 and check again" }));
    await within(dialog).findByText("Step 3 of 5 · What it can do");
    expect(run.mock.calls[1][0].values.address).toBe("http://192.168.1.50:8080/v1");
  });
});
