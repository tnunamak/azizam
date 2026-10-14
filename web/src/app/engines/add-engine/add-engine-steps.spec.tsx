import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/lib/api";
import { enginesApi } from "@/lib/engines-api";
import {
  check,
  definitions,
  renderWithClient,
  testResult,
  workflowRead,
  workflowText,
} from "../__fixtures__/test-utils";
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

async function toStep3(user: User, kind: RegExp, checkName: string, address: string, path?: string) {
  vi.spyOn(enginesApi, "check").mockResolvedValue(check(checkName));
  mount();
  await user.click(await screen.findByRole("button", { name: kind }));
  const dialog = screen.getByRole("dialog", { name: "Add engine" });
  await user.type(within(dialog).getByLabelText(/^Address/), address);
  if (path) await user.type(within(dialog).getByLabelText(/^Path on the server/), path);
  await user.click(within(dialog).getByRole("button", { name: "Continue" }));
  await within(dialog).findByText(/^Step 3 of 5/);
  return dialog;
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

describe("f: what a detected server can do", () => {
  it("sorts rows by level, ticks Likely, leaves Possible unticked with the reason", async () => {
    const user = userEvent.setup();
    const dialog = await toStep3(user, /Any OpenAI-compatible server/, "oai_ok", "http://192.168.1.50:8080/v1");
    expect(within(dialog).getByText("Reached the server. No key needed. Found 3 models.")).toBeVisible();
    const group = within(dialog).getByRole("group", { name: "What this server can do" });
    const boxes = within(group).getAllByRole("checkbox");
    expect(boxes.map((b) => b.getAttribute("aria-label") ?? b.closest("label")?.textContent)).toEqual([
      "Chat",
      "Embeddings",
      "Images",
      "Reranking",
      "Speech to text",
      "Text to speech",
      "Live voice",
    ]);
    expect(within(group).getByRole("checkbox", { name: "Chat" })).toBeChecked();
    expect(within(group).getByRole("checkbox", { name: "Images" })).not.toBeChecked();
    expect(within(group).getAllByText("Likely")).toHaveLength(2);
    expect(within(group).getByText("Possible")).toBeVisible();
    expect(within(group).getByText("Not supported")).toBeVisible();
    expect(within(group).getAllByText("Not found")).toHaveLength(3);
    expect(within(group).getByText(/A “422 Unprocessable Content” answer shows the route exists/)).toBeVisible();
    expect(within(group).getAllByText("Why unticked:").length).toBeGreaterThan(0);
    await user.click(within(group).getByRole("checkbox", { name: "Images" }));
    expect(within(group).getByText("Not proven yet. Run a real test to be sure.")).toBeVisible();
    expect(within(dialog).getByText(/A real test sends one tiny request from the gateway’s computer/)).toBeVisible();
    expect(within(dialog).getByText("The server’s own details say so, or a real test passed. Ticked for you.")).toBeVisible();
    expect(
      within(dialog).getByText(
        "In a hurry? “Add now with these settings” adds what is ticked, every model the server listed, and the name “Engine at 192.168.1.50:8080”.",
      ),
    ).toBeVisible();
  });

  it("f2: a passing test on a Possible row ticks it and proves it", async () => {
    const user = userEvent.setup();
    const dialog = await toStep3(user, /Any OpenAI-compatible server/, "oai_ok", "http://192.168.1.50:8080/v1");
    const result = testResult("images_pass");
    const test = vi.spyOn(enginesApi, "test").mockResolvedValue(result);
    await user.click(within(dialog).getByRole("button", { name: "Run a real test for Images" }));
    const at = clockTime(result.at);
    expect(await within(dialog).findByText(`Test passed at ${at}. Ticked for you.`)).toBeVisible();
    expect(test.mock.calls[0][0]).toMatchObject({ definition: "openai-compatible", capability: "images" });
    expect(test.mock.calls[0][0]).not.toHaveProperty("service_id");
    const group = within(dialog).getByRole("group", { name: "What this server can do" });
    expect(within(group).getByRole("checkbox", { name: "Images" })).toBeChecked();
    expect(within(group).getAllByText("Likely")).toHaveLength(3);
    expect(within(group).getByText(`A real test passed at ${at}. That shows the server can do this.`)).toBeVisible();
    expect(within(group).getByText("One request for a 64-pixel image of “a red dot”")).toBeVisible();
    expect(within(group).getByText("POST http://192.168.1.50:8080/v1/images/generations")).toBeVisible();
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
    const pub = within(dialog).getAllByLabelText("Public name");
    expect(pub[0]).toHaveValue("qwen3-32b");
    expect(within(dialog).getAllByText("Suggested").length).toBeGreaterThan(2);
    expect(within(dialog).getAllByLabelText("Context window")[0]).toHaveValue("40960");
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
    expect(within(dialog).getAllByLabelText("Public name")).toHaveLength(1);
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
    expect(recap).toHaveTextContent("KeyNone. The server has no key.");
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
    await user.click(within(dialog).getByRole("button", { name: "Add now with these settings" }));
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
    await user.click(within(dialog).getByRole("button", { name: "Add now with these settings" }));
    expect(await within(dialog).findByText("Server not reachable")).toBeVisible();
    expect(within(dialog).getByText("Step 2 of 5 · Address and key")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Add but keep off" })).toBeVisible();
  });
});

describe("declared kinds: speech with a custom path", () => {
  it("f: one You chose it row; P3: a failed real test shows what was sent and the server's answer", async () => {
    const user = userEvent.setup();
    const dialog = await toStep3(user, /Speech-to-text server with a custom path/, "stt_ok", "http://192.168.1.60:8178", "/inference");
    expect(within(dialog).getByRole("heading", { name: "What this server will do" })).toBeVisible();
    expect(within(dialog).getByText("Reached the server. No key needed. The path answered.")).toBeVisible();
    const group = within(dialog).getByRole("group", { name: "What this server can do" });
    expect(within(group).getAllByRole("checkbox")).toHaveLength(1);
    expect(within(group).getByRole("checkbox", { name: "Speech to text" })).toBeChecked();
    expect(within(group).getByText("You chose it")).toBeVisible();
    expect(within(group).getByText(/An empty request to that path answered “400 Bad Request: no file provided”/)).toBeVisible();
    expect(within(dialog).queryByText("Possible")).toBeNull();
    const failed = testResult("stt_fail");
    vi.spyOn(enginesApi, "test").mockResolvedValue(failed);
    await user.click(within(group).getByRole("button", { name: "Run a real test for Speech to text" }));
    expect(await within(group).findByText(`Test failed at ${clockTime(failed.at)}. The server said no.`)).toBeVisible();
    expect(within(group).getByText("The server answered “400 Bad Request” at POST http://192.168.1.60:8178/inference.")).toBeVisible();
    expect(within(group).getByText(/Azizam sent a multipart form with one second of silence in the field “file”/)).toBeVisible();
    expect(within(group).getByText("Check what the server expects with whoever set it up. Its own text is under Details.")).toBeVisible();
  });

  it("g: names the model and its voices; the recap says what will be called", async () => {
    const user = userEvent.setup();
    const dialog = await toStep3(user, /Text-to-speech server with a custom path/, "tts_ok", "http://192.168.1.60:5002", "/api/tts");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(within(dialog).getByRole("heading", { name: "Name the model and its voices" })).toBeVisible();
    expect(within(dialog).getByText("Step 4 of 5 · Model and voices")).toBeVisible();
    expect(within(dialog).getByLabelText(/^Public name/)).toHaveValue("local-voice");
    expect(within(dialog).getByText("No voices listed. Azizam sends no voice name, and the server uses its own.")).toBeVisible();
    for (const voice of ["en_US-lessac-medium", "en_GB-alan-low"]) {
      await user.type(within(dialog).getByLabelText("Add a voice by name"), voice);
      await user.click(within(dialog).getByRole("button", { name: "Add voice" }));
    }
    expect(within(dialog).getByLabelText("Voice when an app names none")).toHaveValue("en_US-lessac-medium");
    await user.type(within(dialog).getByLabelText("Name the server uses (optional)"), "en_US-lessac");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    const recap = within(dialog).getByTestId("recap");
    expect(recap).toHaveTextContent("Path/api/tts");
    expect(recap).toHaveTextContent("Azizam will callPOST http://192.168.1.60:5002/api/tts");
    expect(recap).toHaveTextContent("Modellocal-voice, which the server calls “en_US-lessac”");
    expect(recap).toHaveTextContent("Voicesen_US-lessac-medium, en_GB-alan-low");
    expect(recap).toHaveTextContent("TestedNot tested");
    const create = vi.spyOn(enginesApi, "create").mockResolvedValue(registryWith("tts", "Text to speech at 192.168.1.60:5002"));
    await user.click(within(dialog).getByRole("button", { name: "Add engine" }));
    await waitFor(() => expect(create).toHaveBeenCalled());
    expect(create.mock.calls[0][0]).toMatchObject({
      definition: "text-to-speech-custom-path",
      capabilities: ["tts"],
      values: {
        address: "http://192.168.1.60:5002",
        path: "/api/tts",
        public_name: "local-voice",
        server_model: "en_US-lessac",
        voices: ["en_US-lessac-medium", "en_GB-alan-low"],
        default_voice: "en_US-lessac-medium",
      },
    });
    expect(create.mock.calls[0][0]).not.toHaveProperty("models");
  });
});

describe("workflow kind: node-graph server", () => {
  async function toFile(user: User) {
    const dialog = await toStep3(user, /Node-graph image and video server/, "graph_ok", "http://192.168.1.50:8188");
    return dialog;
  }
  function upload(dialog: HTMLElement, name: string, text: string) {
    const input = within(dialog).getByLabelText("Choose a file", { selector: "input" });
    fireEvent.change(input, { target: { files: [new File([text], name, { type: "application/json" })] } });
  }

  it("f: picks a file from the browser and reads it; f3: a UI-format file offers another file or keep off", async () => {
    const user = userEvent.setup();
    const dialog = await toFile(user);
    expect(within(dialog).getByText("Step 3 of 5 · Workflow file")).toBeVisible();
    expect(within(dialog).getByRole("heading", { name: "Pick the workflow" })).toBeVisible();
    expect(within(dialog).getByText("A .json file in API format, up to 20 MB.")).toBeVisible();
    expect(within(dialog).queryByText(/Saved on the server/)).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeDisabled();
    const read = vi.spyOn(enginesApi, "workflow").mockResolvedValueOnce(workflowRead("bad"));
    upload(dialog, "dream-v2.json", '{"nodes":[],"links":[]}');
    expect(await within(dialog).findByText("This file is not in API format")).toBeVisible();
    expect(read.mock.calls[0][0]).toEqual({ definition: "node-graph", file: "dream-v2.json", text: '{"nodes":[],"links":[]}' });
    expect(within(dialog).getByText("“Add but keep off” adds the engine turned off, without a workflow. You choose one later, on the engine page.")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Add but keep off" })).toBeVisible();
    read.mockResolvedValueOnce(workflowRead("ok"));
    upload(dialog, "sdxl-text-to-image.json", workflowText);
    expect(await within(dialog).findByText("Read in API format. 7 nodes found.")).toBeVisible();
    expect(within(dialog).getByLabelText("This workflow makes")).toHaveValue("images");
    expect(within(dialog).getByRole("button", { name: "Choose another file" })).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeEnabled();
  });

  it("g: matches inputs, N1 asks for the name apps use, and the recap and create carry it", async () => {
    const user = userEvent.setup();
    const dialog = await toFile(user);
    vi.spyOn(enginesApi, "workflow").mockResolvedValue(workflowRead("ok"));
    upload(dialog, "sdxl-text-to-image.json", workflowText);
    await within(dialog).findByText("Read in API format. 7 nodes found.");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(within(dialog).getByText("Step 4 of 5 · Inputs")).toBeVisible();
    expect(within(dialog).getByRole("heading", { name: "Match the inputs" })).toBeVisible();
    const prompt = within(dialog).getByLabelText("Workflow field that fills Prompt");
    expect(prompt).toHaveValue("6.text");
    expect(within(dialog).getByText("Matched on its own: node 6, “Positive prompt”, feeds the sampler’s positive input.")).toBeVisible();
    expect(within(dialog).getByText("This file has no node that loads an image, so image-in stays off.")).toBeVisible();
    expect(within(dialog).getByLabelText("Workflow field that fills Image in")).toBeDisabled();
    const publicName = within(dialog).getByLabelText(/^Name apps use/);
    expect(publicName).toHaveValue("sdxl-text-to-image");
    expect(within(dialog).getByText(/Apps ask for this workflow by this name/)).toBeVisible();
    await user.selectOptions(prompt, "");
    expect(within(dialog).getByText("Choose one")).toBeVisible();
    expect(within(dialog).getByText("Azizam needs to know where the prompt goes. Choose the field that holds the prompt text.")).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeDisabled();
    await user.selectOptions(prompt, "7.text");
    expect(within(dialog).getByText("You chose")).toBeVisible();
    await user.selectOptions(prompt, "6.text");
    await user.clear(publicName);
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeDisabled();
    await user.type(publicName, "sdxl");
    const passed = testResult("graph_pass");
    const test = vi.spyOn(enginesApi, "test").mockResolvedValue(passed);
    await user.click(within(dialog).getByRole("button", { name: "Run a real test" }));
    expect(await within(dialog).findByText(`Test passed at ${clockTime(passed.at)}.`)).toBeVisible();
    expect(test.mock.calls[0][0]).toMatchObject({ definition: "node-graph", capability: "graph_images", inputs: { prompt: "6.text", seed: "3.seed" } });
    expect(test.mock.calls[0][0].workflow).toHaveProperty("3");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    const recap = within(dialog).getByTestId("recap");
    expect(recap).toHaveTextContent("Workflowsdxl-text-to-image.json (7 nodes)");
    expect(recap).toHaveTextContent("Name apps usesdxl");
    expect(recap).toHaveTextContent("Will offerImages");
    expect(recap).toHaveTextContent("Inputs filledPrompt, Seed, Width, Height, Steps, Model");
    expect(recap).toHaveTextContent("Not usedImage in");
    expect(recap).toHaveTextContent(`TestedWorkflow checked at ${clockTime(passed.at)}`);
    const create = vi.spyOn(enginesApi, "create").mockResolvedValue(registryWith("rtx-4090-workflows", "RTX 4090 workflows"));
    await user.click(within(dialog).getByRole("button", { name: "Add engine" }));
    await waitFor(() => expect(create).toHaveBeenCalled());
    const body = create.mock.calls[0][0];
    expect(body).toMatchObject({
      definition: "node-graph",
      name: "RTX 4090 workflows",
      capabilities: ["graph_images"],
      values: {
        address: "http://192.168.1.50:8188",
        workflow_public_name: "sdxl",
        workflow_file: "sdxl-text-to-image.json",
        workflow_makes: "images",
        workflow_inputs: { prompt: "6.text", seed: "3.seed", width: "5.width", height: "5.height", steps: "3.steps", model: "4.ckpt_name" },
      },
    });
    expect(body.values.workflow_graph).toHaveProperty("9");
  });
});
