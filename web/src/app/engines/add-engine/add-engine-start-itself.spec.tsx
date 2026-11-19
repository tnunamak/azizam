import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/lib/api";
import {
  engineFoldersApi,
  enginesApi,
  type EngineDefinition,
  type EngineDefinitions,
  type ProcessPreview,
} from "@/lib/engines-api";
import { definition, definitions, renderWithClient } from "../__fixtures__/test-utils";
import { AddEngineDialog } from "./add-engine-dialog";

const viewer = vi.hoisted(() => ({
  current: { authenticated: true, name: "Avery", email: null, role: "owner", admin: true },
}));
vi.mock("@/components/shell/session", () => ({ useViewer: () => viewer.current }));

const INSTALL = "/srv/engines/studio";
const COMMAND = [`${INSTALL}/.venv/bin/python`, `${INSTALL}/main.py`, "--port", "8101"];

/** A kind Azizam starts: the catalog names its slots, and the command comes from the dry run. */
function startsItself(): EngineDefinition {
  return {
    ...structuredClone(definition("openai-compatible")),
    definition: "workflow-image-engine",
    title: "Workflow image engine",
    summary: "Makes images from workflows. Azizam starts it from a folder you choose.",
    badge: undefined,
    keywords: [],
    steps: ["Kind", "Address", "What it can do", "Workflows", "Name"],
    capabilities: [{ id: "images", label: "Pictures", kinds: ["image"] }],
    process_slots: {
      folder: { type: "path", kind: "dir", locate: ["main.py"] },
      port: { type: "port", kind: null, locate: [] },
    },
  };
}

function catalog(): EngineDefinitions {
  const copy = structuredClone(definitions);
  copy.definitions.push(startsItself());
  return copy;
}

const ready = (port = 8101): ProcessPreview => ({ argv: COMMAND, cwd: INSTALL, port, problems: [] });
const refused = (...problems: ProcessPreview["problems"]): ProcessPreview => ({ argv: [], cwd: null, port: null, problems });

const onAdded = vi.fn();

function mount() {
  return renderWithClient(
    <AddEngineDialog open onOpenChange={() => undefined} onAdded={onAdded} onOpenEngine={() => undefined} checkRevealMs={0} />,
  );
}

/** The dry run as the gateway answers it: port 8100 is taken, the folder must exist. */
function dryRun(body: { slots: Record<string, string | number> }): ProcessPreview {
  const { folder, port } = body.slots;
  if (port === 8100 && folder === undefined) return refused({ code: "port_busy", slot: "port", why: "port: something already uses port 8100. Pick another." }, { code: "missing_slot", slot: "folder", why: "folder: give a value." });
  if (folder === undefined) return refused({ code: "missing_slot", slot: "folder", why: "folder: give a value." });
  if (folder === INSTALL) return ready(Number(port));
  return refused({ code: "missing", slot: "folder", why: "folder: nothing exists at that path." });
}

async function openSlots(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: /Workflow image engine/ }));
  return screen.getByRole("dialog", { name: "Add engine" });
}

beforeEach(() => {
  viewer.current = { authenticated: true, name: "Avery", email: null, role: "owner", admin: true };
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(catalog());
  vi.spyOn(enginesApi, "processPreview").mockImplementation(async (body) => dryRun(body));
  vi.spyOn(engineFoldersApi, "get").mockResolvedValue({ folders: ["/srv/engines"], revision: "1" });
  vi.spyOn(enginesApi, "access").mockResolvedValue({
    plans: [], you: true, not_included: [], category_rules: false, unreadable_plans: 0, sentence: "Only you.", link: "/access",
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  onAdded.mockReset();
});

describe("an engine Azizam starts", () => {
  it("asks for the slots the catalog names and fills the port with a free one", async () => {
    const user = userEvent.setup();
    mount();
    const dialog = await openSlots(user);
    expect(within(dialog).getByText("Step 2 of 3 · Start")).toBeVisible();
    expect(within(dialog).getByLabelText(/^Folder/)).toHaveValue("");
    // 8100 is taken, so the first free one is offered, and the owner can still change it.
    await waitFor(() => expect(within(dialog).getByLabelText(/^Port/)).toHaveValue("8101"));
    expect(within(dialog).getByText(/It must hold main\.py\./)).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeDisabled();
    expect(within(dialog).queryByLabelText(/^Address/)).toBeNull();
  });

  it("shows the exact command, folder and port once the owner stops typing, and asks nothing else of the gateway first", async () => {
    const user = userEvent.setup();
    mount();
    const dialog = await openSlots(user);
    await waitFor(() => expect(within(dialog).getByLabelText(/^Port/)).toHaveValue("8101"));
    await user.type(within(dialog).getByLabelText(/^Folder/), INSTALL);
    const preview = await within(dialog).findByTestId("process-preview", undefined, { timeout: 3000 });
    expect(preview).toHaveTextContent(COMMAND.join(" "));
    expect(preview).toHaveTextContent(`Working folder${INSTALL}`);
    expect(preview).toHaveTextContent("Port8101");
    const slotsSent = vi.mocked(enginesApi.processPreview).mock.calls.map(([body]) => body.slots);
    expect(slotsSent.at(-1)).toEqual({ folder: INSTALL, port: 8101 });
    // One call per pause in typing, not one per key.
    expect(slotsSent.filter((slots) => slots.folder !== undefined).length).toBeLessThan(INSTALL.length);
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeEnabled();
  });

  it("puts each refusal next to its own slot and keeps Continue off", async () => {
    const user = userEvent.setup();
    mount();
    const dialog = await openSlots(user);
    await waitFor(() => expect(within(dialog).getByLabelText(/^Port/)).toHaveValue("8101"));
    await user.type(within(dialog).getByLabelText(/^Folder/), "/srv/engines/nothing");
    const folder = within(dialog).getByLabelText(/^Folder/);
    await waitFor(() => expect(folder).toHaveAccessibleDescription("Nothing exists at that path."), { timeout: 3000 });
    expect(folder).toBeInvalid();
    expect(within(dialog).getByLabelText(/^Port/)).not.toBeInvalid();
    expect(within(dialog).queryByTestId("process-preview")).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeDisabled();
  });

  it("says when no engine folders are set and links to the setting", async () => {
    vi.mocked(engineFoldersApi.get).mockResolvedValue({ folders: [], revision: "0" });
    vi.mocked(enginesApi.processPreview).mockResolvedValue(
      refused({ code: "no_folders", slot: "folder", why: "Add this folder to the folders Azizam may run engines from first." }),
    );
    const user = userEvent.setup();
    mount();
    const dialog = await openSlots(user);
    const note = await within(dialog).findByText(/may not start engines from any folder yet/);
    expect(within(note.closest("div") as HTMLElement).getByRole("link", { name: /Engine folders/ })).toHaveAttribute(
      "href",
      "/settings#engine-folders",
    );
  });

  it("saves with the slots and no address, after the command is shown on the last step", async () => {
    const create = vi.spyOn(enginesApi, "create").mockResolvedValue({
      registry: { services: { studio: { name: "studio" } } } as never,
      serviceId: "studio",
    });
    const user = userEvent.setup();
    mount();
    const dialog = await openSlots(user);
    await waitFor(() => expect(within(dialog).getByLabelText(/^Port/)).toHaveValue("8101"));
    await user.type(within(dialog).getByLabelText(/^Folder/), INSTALL);
    await within(dialog).findByTestId("process-preview", undefined, { timeout: 3000 });
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(within(dialog).getByText("Step 3 of 3 · Name")).toBeVisible();
    expect(within(dialog).getByLabelText(/^Name/)).toHaveValue("studio");
    expect(within(dialog).getByTestId("recap")).toHaveTextContent(COMMAND.join(" "));
    await user.click(within(dialog).getByRole("button", { name: "Add engine" }));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0]).toEqual({
      definition: "workflow-image-engine",
      name: "studio",
      values: {},
      slots: { folder: INSTALL, port: 8101 },
    });
    await waitFor(() => expect(onAdded).toHaveBeenCalledTimes(1));
  });

  it("returns to the slot when the gateway refuses one at save", async () => {
    vi.spyOn(enginesApi, "create").mockRejectedValue(
      new ApiError("process_template_refused", "folder: this is not inside a folder Azizam may run engines from.", 422, {
        error: { field: "folder" },
      }),
    );
    const user = userEvent.setup();
    mount();
    const dialog = await openSlots(user);
    await waitFor(() => expect(within(dialog).getByLabelText(/^Port/)).toHaveValue("8101"));
    await user.type(within(dialog).getByLabelText(/^Folder/), INSTALL);
    await within(dialog).findByTestId("process-preview", undefined, { timeout: 3000 });
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    await user.click(within(dialog).getByRole("button", { name: "Add engine" }));
    expect(await within(dialog).findByText("Step 2 of 3 · Start")).toBeVisible();
    expect(within(dialog).getByLabelText(/^Folder/)).toHaveAccessibleDescription(/not inside a folder Azizam may run engines from/);
  });

  it("does not offer the kind to anyone but the owner", async () => {
    viewer.current = { authenticated: true, name: "Sam", email: null, role: "admin", admin: true };
    mount();
    const dialog = await screen.findByRole("dialog", { name: "Add engine" });
    const kinds = within(await within(dialog).findByRole("group", { name: "Server kinds" })).getAllByRole("button");
    expect(kinds.map((kind) => kind.textContent).join(" ")).not.toMatch(/Workflow image engine/);
    expect(engineFoldersApi.get).not.toHaveBeenCalled();
    expect(enginesApi.processPreview).not.toHaveBeenCalled();
  });
});
