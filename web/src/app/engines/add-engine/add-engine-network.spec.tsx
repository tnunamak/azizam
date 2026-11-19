import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/lib/api";
import { enginesApi, type CheckResult, type EngineDefinitions, type RemotePlan } from "@/lib/engines-api";
import { accessAnswer, check, definition, definitions, renderWithClient } from "../__fixtures__/test-utils";
import { AddEngineDialog } from "./add-engine-dialog";

const viewer = vi.hoisted(() => ({
  current: { authenticated: true, name: "Avery", email: null, role: "owner", admin: true },
}));
vi.mock("@/components/shell/session", () => ({ useViewer: () => viewer.current }));

const ADDRESS = "http://192.0.2.5:7860";
const PLAIN: RemotePlan = { network: "10.0.0.0/24", transport: "private_plaintext", trust_needed: true, trusted: false };
const TLS: RemotePlan = { network: "192.0.2.0/24", transport: "tls", trust_needed: false, trusted: false };

/** A kind with the two owner-only fields the sdapi definition ships. */
function networkCatalog(): EngineDefinitions {
  const copy = structuredClone(definitions);
  const base = structuredClone(definition("openai-compatible"));
  copy.definitions.push({
    ...base,
    definition: "sdapi",
    title: "Picture server (sdapi)",
    keywords: [],
    badge: undefined,
    fields: [
      ...base.fields,
      {
        key: "where",
        label: "Where does it run?",
        type: "select",
        default: "this",
        order: 2,
        owner_only: true,
        options: [
          { value: "this", label: "On this computer" },
          { value: "network", label: "On another computer on my network" },
        ],
      },
      {
        key: "network_trusted",
        label: "I trust this network",
        type: "boolean",
        default: false,
        order: 3,
        owner_only: true,
        show_if: { field: "where", equals: "network" },
      },
    ],
  });
  return copy;
}

const onAdded = vi.fn();
type User = ReturnType<typeof userEvent.setup>;

function answer(remote?: RemotePlan): CheckResult {
  return { ...check("oai_ok"), ...(remote ? { remote } : {}) };
}

function mount() {
  return renderWithClient(
    <AddEngineDialog open onOpenChange={() => undefined} onAdded={onAdded} onOpenEngine={() => undefined} checkRevealMs={0} />,
  );
}

async function toCheckStep(user: User, remote?: RemotePlan) {
  vi.spyOn(enginesApi, "check").mockResolvedValue(answer(remote));
  mount();
  await user.click(await screen.findByRole("button", { name: /Picture server/ }));
  const dialog = screen.getByRole("dialog", { name: "Add engine" });
  await user.type(within(dialog).getByLabelText(/^Address/), ADDRESS);
  if (viewer.current.role === "owner") await user.selectOptions(within(dialog).getByLabelText("Where does it run?"), "network");
  await user.click(within(dialog).getByRole("button", { name: "Continue" }));
  await within(dialog).findByText(/^Step 3 of 5/);
  return dialog;
}

beforeEach(() => {
  viewer.current = { authenticated: true, name: "Avery", email: null, role: "owner", admin: true };
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(networkCatalog());
  vi.spyOn(enginesApi, "access").mockResolvedValue(accessAnswer("you_and_family"));
});
afterEach(() => {
  cleanup();
  onAdded.mockReset();
  vi.restoreAllMocks();
});

describe("a server on another computer", () => {
  it("sends where with the check and shows the network and the plain http warning beside the trust box", async () => {
    const user = userEvent.setup();
    const dialog = await toCheckStep(user, PLAIN);
    expect(enginesApi.check).toHaveBeenCalledWith(
      { definition: "sdapi", values: { address: ADDRESS, where: "network" } },
      expect.anything(),
    );
    const block = within(dialog).getByRole("group", { name: "The network you are asked to trust" });
    expect(within(block).getByText("Azizam will call 10.0.0.0/24 on your network")).toBeVisible();
    expect(
      within(block).getByText("with plain http: anyone on that network can read what Azizam and the server send each other"),
    ).toBeVisible();
    expect(within(block).getByRole("checkbox", { name: "I trust this network" })).not.toBeChecked();
  });

  it("says over https for tls and asks for no trust", async () => {
    const user = userEvent.setup();
    const dialog = await toCheckStep(user, TLS);
    const block = within(dialog).getByRole("group", { name: "The network you are asked to trust" });
    expect(within(block).getByText("Azizam will call 192.0.2.0/24 on your network")).toBeVisible();
    expect(within(block).getByText("over https")).toBeVisible();
    expect(within(block).queryByRole("checkbox")).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeEnabled();
    expect(within(dialog).getByRole("button", { name: "Add engine now" })).toBeEnabled();
  });

  it("keeps every save off for plain http until trust is ticked, then sends it", async () => {
    const user = userEvent.setup();
    const dialog = await toCheckStep(user, PLAIN);
    const reason = "Tick “I trust this network” to add this server. It is reached with plain http.";
    expect(within(dialog).getByText(reason)).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Add engine now" })).toBeDisabled();
    await user.click(within(dialog).getByRole("checkbox", { name: "I trust this network" }));
    expect(within(dialog).queryByText(reason)).toBeNull();
    expect(within(dialog).getByRole("button", { name: "Continue" })).toBeEnabled();
    const create = vi.spyOn(enginesApi, "create").mockResolvedValue({ registry: { schema_version: 1, services: {} }, serviceId: "x" } as never);
    await user.click(within(dialog).getByRole("button", { name: "Add engine now" }));
    await waitFor(() => expect(create).toHaveBeenCalled());
    expect(create.mock.calls[0][0].values).toMatchObject({ address: ADDRESS, where: "network", network_trusted: true });
  });

  it("shows a save refusal for the trust box beside it", async () => {
    const user = userEvent.setup();
    const dialog = await toCheckStep(user, PLAIN);
    await user.click(within(dialog).getByRole("checkbox", { name: "I trust this network" }));
    const message = "Confirm that you trust the network 10.0.0.0/24. This server is reached with plain http.";
    vi.spyOn(enginesApi, "create").mockRejectedValue(new ApiError("invalid_request", message, 400, { error: { field: "network_trusted" } }));
    await user.click(within(dialog).getByRole("button", { name: "Add engine now" }));
    const block = await within(dialog).findByRole("group", { name: "The network you are asked to trust" });
    expect(within(block).getByText(message)).toBeVisible();
  });

  it("shows a refusal for where beside the where select, on the address step", async () => {
    const user = userEvent.setup();
    const dialog = await toCheckStep(user, TLS);
    const message = "That address is this computer. Choose “On this computer” instead.";
    vi.spyOn(enginesApi, "create").mockRejectedValue(new ApiError("invalid_request", message, 400, { error: { field: "where" } }));
    await user.click(within(dialog).getByRole("button", { name: "Add engine now" }));
    const select = await within(dialog).findByLabelText("Where does it run?");
    expect(select).toHaveAccessibleDescription(message);
  });

  it.each([
    ["admin", { authenticated: true, name: "Sam", email: null, role: "admin", admin: true }],
    ["member", { authenticated: true, name: "Mo", email: null, role: "member", admin: false }],
  ])("never shows where to a %s, and never sends it", async (_label, who) => {
    viewer.current = who;
    const user = userEvent.setup();
    const dialog = await toCheckStep(user);
    expect(enginesApi.check).toHaveBeenCalledWith({ definition: "sdapi", values: { address: ADDRESS } }, expect.anything());
    expect(within(dialog).queryByText(/Where does it run/)).toBeNull();
    expect(within(dialog).queryByText(/trust this network/)).toBeNull();
  });
});
