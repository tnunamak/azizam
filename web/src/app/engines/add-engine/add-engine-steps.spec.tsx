import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/lib/api";
import { enginesApi } from "@/lib/engines-api";
import { accessAnswer, check, definitions, renderWithClient, testResult } from "../__fixtures__/test-utils";
import { clockTime } from "./draft";
import { AddEngineDialog } from "./add-engine-dialog";

const viewer = vi.hoisted(() => ({
  current: { authenticated: true, name: "Tim", email: null, role: "owner", admin: true },
}));
vi.mock("@/components/shell/session", () => ({ useViewer: () => viewer.current }));

const onAdded = vi.fn();
const onOpenEngine = vi.fn();
type User = ReturnType<typeof userEvent.setup>;

function mount() {
  return renderWithClient(
    <AddEngineDialog open onOpenChange={() => undefined} onAdded={onAdded} onOpenEngine={onOpenEngine} checkRevealMs={0} />,
  );
}

function registryWith(id: string, name: string) {
  return { registry: { schema_version: 1, services: { [id]: { id, name } } }, serviceId: id } as never;
}

async function toStep3(user: User, kind: RegExp, checkName: string, address: string) {
  vi.spyOn(enginesApi, "check").mockResolvedValue(check(checkName));
  mount();
  await user.click(await screen.findByRole("button", { name: kind }));
  const dialog = screen.getByRole("dialog", { name: "Add engine" });
  await user.type(within(dialog).getByLabelText(/^Address/), address);
  await user.click(within(dialog).getByRole("button", { name: "Continue" }));
  await within(dialog).findByText(/^Step 3 of 5/);
  return dialog;
}

beforeEach(() => {
  viewer.current = { authenticated: true, name: "Tim", email: null, role: "owner", admin: true };
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(structuredClone(definitions));
  vi.spyOn(enginesApi, "access").mockResolvedValue(accessAnswer("you_and_family"));
});
afterEach(() => {
  cleanup();
  onAdded.mockReset();
  onOpenEngine.mockReset();
});

describe("f: what a detected server can do", () => {
  it("sorts rows by level, ticks Likely, leaves Possible unticked with the reason", async () => {
    const user = userEvent.setup();
    const dialog = await toStep3(user, /Any OpenAI-compatible server/, "oai_ok", "http://192.168.1.50:8080/v1");
    expect(within(dialog).getByText("Reached the server. No key needed. Found 3 models.")).toBeVisible();
    const group = within(dialog).getByRole("group", { name: "What this server can do" });
    const boxes = within(group).getAllByRole("checkbox");
    expect(boxes.map((b) => b.getAttribute("aria-label") ?? b.closest("label")?.textContent)).toEqual(["Chat", "Embeddings"]);
    expect(within(group).getByRole("checkbox", { name: "Chat" })).toBeChecked();
    expect(within(group).getByRole("checkbox", { name: "Embeddings" })).toBeChecked();
    expect(within(group).getAllByText("Likely")).toHaveLength(2);
    for (const gone of ["Images", "Reranking", "Speech to text", "Text to speech", "Live voice"]) {
      expect(within(group).queryByText(gone)).toBeNull();
    }
    expect(within(dialog).queryByText(/Workflow|node-graph/i)).toBeNull();
    expect(within(dialog).getByText(/A real test sends one tiny request from the gateway’s computer/)).toBeVisible();
    expect(within(dialog).getByText("The server’s own details say so, or a real test passed. Ticked for you.")).toBeVisible();
    expect(
      within(dialog).getByText(
        "In a hurry? “Add engine now” adds what is ticked, every model the server listed, and the name “Engine at 192.168.1.50:8080”.",
      ),
    ).toBeVisible();
  });

  it("f2: a passing test on a Possible row ticks it and proves it", async () => {
    const user = userEvent.setup();
    const possible = check("oai_ok");
    const row = possible.capabilities.find((c) => c.id === "embeddings");
    if (!row) throw new Error("fixture has no embeddings row");
    Object.assign(row, { level: "possible", ticked: false, evidence: "The route answered.", why: "Nothing proves it." });
    vi.spyOn(enginesApi, "check").mockResolvedValue(possible);
    mount();
    await user.click(await screen.findByRole("button", { name: /Any OpenAI-compatible server/ }));
    const dialog = screen.getByRole("dialog", { name: "Add engine" });
    await user.type(within(dialog).getByLabelText(/^Address/), "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await within(dialog).findByText(/^Step 3 of 5/);
    expect(within(dialog).getByRole("checkbox", { name: "Embeddings" })).not.toBeChecked();
    const result = testResult("embeddings_pass");
    const test = vi.spyOn(enginesApi, "test").mockResolvedValue(result);
    await user.click(within(dialog).getByRole("button", { name: "Run a real test for Embeddings" }));
    const at = clockTime(result.at);
    expect(await within(dialog).findByText(`Test passed at ${at}. Ticked for you.`)).toBeVisible();
    expect(test.mock.calls[0][0]).toMatchObject({ definition: "openai-compatible", capability: "embeddings" });
    expect(test.mock.calls[0][0]).not.toHaveProperty("service_id");
    const group = within(dialog).getByRole("group", { name: "What this server can do" });
    expect(within(group).getByRole("checkbox", { name: "Embeddings" })).toBeChecked();
    expect(within(group).getAllByText("Likely")).toHaveLength(2);
    expect(within(group).getByText(/^A test of embeddings.*passed at .* using unsaved settings\./)).toBeVisible();
    expect(within(group).getByText("POST http://192.168.1.50:8080/v1/embeddings")).toBeVisible();
    expect(
      within(group).getByText("Ran on the gateway’s computer with the values on this screen. Nothing was saved, and it is not in Activity."),
    ).toBeVisible();
    expect(within(group).getByRole("button", { name: "Test again" })).toBeVisible();
    expect(within(dialog).queryByText("See it in Activity")).toBeNull();
  });
});

describe("g, h: models, name and create", () => {
  it("g: lists models with suggested public names, counts, search, by-name, and unique names", async () => {
    const user = userEvent.setup();
    const dialog = await toStep3(user, /Any OpenAI-compatible server/, "oai_ok", "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(within(dialog).getByText("Step 4 of 5 · Models")).toBeVisible();
    expect(within(dialog).getByText("3 of 3 models offered")).toBeVisible();
    const pub = within(dialog).getAllByLabelText("Name apps use");
    expect(pub[0]).toHaveValue("qwen3-32b");
    expect(within(dialog).getAllByText("Suggested").length).toBeGreaterThan(2);
    expect(within(dialog).getAllByLabelText("Context limit (tokens)")[0]).toHaveValue("40960");
    expect(within(dialog).getAllByText("Reported by server")).toHaveLength(3);
    await user.click(within(dialog).getByRole("checkbox", { name: "Offer gemma-3-12b-it" }));
    expect(within(dialog).getByText("2 of 3 models offered")).toBeVisible();
    await user.type(within(dialog).getByLabelText("Add a model by name"), "mistral-small-3.2");
    await user.click(within(dialog).getByRole("button", { name: "Add model" }));
    expect(within(dialog).getByText("3 of 4 models offered")).toBeVisible();
    expect(within(dialog).getByText("Added by name")).toBeVisible();
    await user.clear(pub[2]);
    await user.type(pub[2], "qwen3-32b");
    expect(within(dialog).getAllByText("Two models use the public name “qwen3-32b”. Each needs its own.").length).toBeGreaterThan(0);
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeDisabled();
    await user.clear(pub[2]);
    await user.type(pub[2], "nomic-embed");
    await user.type(within(dialog).getByRole("searchbox", { name: "Search models" }), "nomic");
    expect(within(dialog).getAllByLabelText("Name apps use")).toHaveLength(1);
  });

  it("h: names the engine, recaps, creates, and hands the page the new id and message", async () => {
    const user = userEvent.setup();
    const dialog = await toStep3(user, /Any OpenAI-compatible server/, "oai_ok", "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(within(dialog).getByText("Step 5 of 5 · Name")).toBeVisible();
    const name = within(dialog).getByLabelText(/^Name/);
    expect(name).toHaveValue("Engine at 192.168.1.50:8080");
    expect(within(dialog).getByText("This server does not name itself, so Azizam used its address. Change it to something you will recognise.")).toBeVisible();
    const recap = within(dialog).getByTestId("recap");
    expect(recap).toHaveTextContent("KindAny OpenAI-compatible server");
    expect(recap).toHaveTextContent("Addresshttp://192.168.1.50:8080/v1");
    expect(recap).toHaveTextContent("KeyNo key supplied.");
    expect(recap).toHaveTextContent("Will offerChat, Embeddings");
    expect(recap).toHaveTextContent("Models3 offered");
    await user.clear(name);
    await user.type(name, "garage-gpu");
    expect(within(dialog).getByText("People see this name on Engines and Status.")).toBeVisible();
    const create = vi.spyOn(enginesApi, "create").mockResolvedValue(registryWith("garage-gpu", "garage-gpu"));
    await user.click(within(dialog).getByRole("button", { name: "Add engine" }));
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
    expect(create.mock.calls[0][0]).toEqual({
      definition: "openai-compatible",
      name: "garage-gpu",
      values: { address: "http://192.168.1.50:8080/v1" },
      capabilities: ["chat", "embeddings"],
      models: [
        { id: "qwen3-32b-instruct-q4_k_m", public_name: "qwen3-32b", context_window: 40960, enabled: true },
        { id: "gemma-3-12b-it", public_name: "gemma-3-12b", context_window: 131072, enabled: true },
        { id: "nomic-embed-text-v1.5", public_name: "nomic-embed-text-v1.5", context_window: 8192, enabled: true },
      ],
      tests: {},
    });
    expect(onAdded.mock.calls[0][0]).toMatchObject({
      serviceId: "garage-gpu",
      message: "Added “garage-gpu”. It is on, with Chat and Embeddings.",
    });
  });

  it("h3: the Name step asks who can use the models, with the ticked capabilities and public names, before Add engine", async () => {
    const user = userEvent.setup();
    const dialog = await toStep3(user, /Any OpenAI-compatible server/, "oai_ok", "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("checkbox", { name: "Embeddings" }));
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    const pub = within(dialog).getAllByLabelText("Name apps use");
    await user.clear(pub[0]);
    await user.type(pub[0], "my-chat");
    await user.click(within(dialog).getByRole("checkbox", { name: "Offer gemma-3-12b-it" }));
    const access = vi.mocked(enginesApi.access);
    access.mockClear();
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(within(dialog).getByText("Step 5 of 5 · Name")).toBeVisible();
    await waitFor(() => expect(access).toHaveBeenCalledOnce());
    // Each model goes with the work the check found it does, so a chat-only plan is not counted for the embedder.
    expect(access).toHaveBeenCalledWith({
      capabilities: ["chat"],
      models: [
        { name: "my-chat", capabilities: ["chat"] },
        { name: "nomic-embed-text-v1.5", capabilities: ["embeddings"] },
      ],
    });
    const answer = await within(dialog).findByRole("heading", { name: "Who can use these models" });
    expect(answer).toBeVisible();
    expect(within(dialog).getByText(/^Usable by: you, and the Family plan/)).toBeVisible();
    expect(within(dialog).getByRole("link", { name: /^Access plans/ })).toBeVisible();
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
    const add = within(dialog).getByRole("button", { name: "Add engine" });
    expect(answer.compareDocumentPosition(add) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("h4: Add engine does not wait for the answer, and the answer is asked again each time the step opens", async () => {
    const user = userEvent.setup();
    const dialog = await toStep3(user, /Any OpenAI-compatible server/, "oai_ok", "http://192.168.1.50:8080/v1");
    const access = vi.mocked(enginesApi.access);
    access.mockClear();
    let answer: (value: ReturnType<typeof accessAnswer>) => void = () => undefined;
    access.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)));
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(await within(dialog).findByText("Checking the access plans…")).toBeVisible();
    const create = vi.spyOn(enginesApi, "create").mockResolvedValue(registryWith("garage-gpu", "garage-gpu"));
    await user.click(within(dialog).getByRole("button", { name: "Add engine" }));
    await waitFor(() => expect(create).toHaveBeenCalledOnce());
    expect(create.mock.calls[0][0]).not.toHaveProperty("access");
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
    // Going back and forward asks again.
    answer(accessAnswer("you_and_family"));
    access.mockResolvedValue(accessAnswer("nobody"));
    await user.click(within(dialog).getByRole("button", { name: "Back" }));
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(await within(dialog).findByText(/^No access plan includes these yet/)).toBeVisible();
    expect(access).toHaveBeenCalledTimes(2);
  });

  it("h5: a failed ask says so, offers Try again, and does not block Add engine", async () => {
    const user = userEvent.setup();
    const dialog = await toStep3(user, /Any OpenAI-compatible server/, "oai_ok", "http://192.168.1.50:8080/v1");
    const access = vi.mocked(enginesApi.access);
    access.mockClear();
    access.mockRejectedValueOnce(new ApiError("internal", "boom", 500, {}));
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(
      await within(dialog).findByText("Azizam couldn’t read the access plans. You can still add the engine; check who can use it on its page."),
    ).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Add engine" })).toBeEnabled();
    await user.click(within(dialog).getByRole("button", { name: "Try again" }));
    expect(await within(dialog).findByText(/^Usable by: you/)).toBeVisible();
    expect(access).toHaveBeenCalledTimes(2);
  });

  it("h2: a duplicate refused at create shows Open and Change the address", async () => {
    const user = userEvent.setup();
    const dialog = await toStep3(user, /Any OpenAI-compatible server/, "oai_ok", "http://192.168.1.50:8080/v1");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    vi.spyOn(enginesApi, "create").mockRejectedValue(
      new ApiError("engine_duplicate", "An engine already uses this address.", 409, {
        error: { code: "engine_duplicate", message: "An engine already uses this address.", field: "address" },
        check: check("oai_dup"),
      }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Add engine" }));
    expect(await within(dialog).findByText("Already added")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Add engine" })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Open Kitchen mini PC" }));
    expect(onOpenEngine).toHaveBeenCalledWith("kitchen");
    await user.click(within(dialog).getByRole("button", { name: "Change the address" }));
    expect(within(dialog).getByText("Step 2 of 5 · Address and key")).toBeVisible();
  });

  it("asks once more before adding over a warning the server raised at create", async () => {
    const user = userEvent.setup();
    const dialog = await toStep3(user, /Any OpenAI-compatible server/, "oai_ok", "http://192.168.1.50:8080/v1");
    const create = vi.spyOn(enginesApi, "create");
    create.mockRejectedValueOnce(
      new ApiError("engine_check_warnings", "warn", 409, {
        error: { code: "engine_check_warnings", message: "warn", field: "address" },
        check: check("oai_nocap"),
      }),
    );
    create.mockResolvedValueOnce(registryWith("engine-at-192-168-1-50-8080", "Engine at 192.168.1.50:8080"));
    await user.click(within(dialog).getByRole("button", { name: "Add engine now" }));
    expect(await within(dialog).findByText("Nothing recognised")).toBeVisible();
    expect(within(dialog).getByText("Step 5 of 5 · Name")).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "Add anyway" }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2));
    expect(create.mock.calls[0][0]).not.toHaveProperty("acknowledge_warnings");
    expect(create.mock.calls[1][0]).toMatchObject({ acknowledge_warnings: true });
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
  });

  it("a server refusal at create goes back to the address with the finding", async () => {
    const user = userEvent.setup();
    const dialog = await toStep3(user, /Any OpenAI-compatible server/, "oai_ok", "http://192.168.1.50:8080/v1");
    vi.spyOn(enginesApi, "create").mockRejectedValue(
      new ApiError("engine_check_failed", "Azizam could not reach the server.", 422, {
        error: { code: "engine_check_failed", message: "Azizam could not reach the server.", field: "address" },
        check: check("oai_down"),
      }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Add engine now" }));
    expect(await within(dialog).findByText("Server not reachable")).toBeVisible();
    expect(within(dialog).getByText("Step 2 of 5 · Address and key")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Add but keep off" })).toBeVisible();
  });
});
