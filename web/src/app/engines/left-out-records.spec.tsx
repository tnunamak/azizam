import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  api,
  ApiError,
  type LeftOutRecord,
  type ServiceRegistry,
} from "@/lib/api";
import Page from "./page";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ admin: true }),
}));

const parsed: LeftOutRecord = {
  section: "services",
  id: "held-engine",
  error: "service 'held-engine' models[0] must be an object",
  parsed: true,
  secret_slots: ["config.headers[0]"],
  record: {
    id: "held-engine",
    name: "held-engine",
    config: {
      host: "10.0.0.9",
      port: 8080,
      models: ["not a model"],
      headers: [{ id: "ent_0123456789abcdef", name: "X-Vendor", configured: true }],
    },
  },
};
const unparsed: LeftOutRecord = {
  section: "protocol_frontends",
  id: "junk-frontend",
  error: "frontend 'junk-frontend' is not an object",
  parsed: false,
  secret_slots: [],
  failed_fields: ["config"],
};

function registry(excluded: LeftOutRecord[]): ServiceRegistry {
  return { schema_version: 2, services: {}, excluded };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <Page />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("records the registry leaves out", () => {
  it("lists each one with its error, its section and an Edit and a Remove action", async () => {
    vi.spyOn(api.services, "registry").mockResolvedValue(registry([parsed, unparsed]));
    vi.spyOn(api, "status").mockResolvedValue({});

    mount();

    const list = await screen.findByRole("region", { name: "Left out" });
    expect(within(list).getByText("held-engine")).toBeTruthy();
    expect(within(list).getByText(/models\[0\] must be an object/)).toBeTruthy();
    expect(within(list).getByText("junk-frontend")).toBeTruthy();
    expect(within(list).getAllByRole("button", { name: /^Edit / })).toHaveLength(2);
    expect(within(list).getAllByRole("button", { name: /^Remove / })).toHaveLength(2);
  });

  it("shows no left-out section when nothing is left out", async () => {
    vi.spyOn(api.services, "registry").mockResolvedValue(registry([]));
    vi.spyOn(api, "status").mockResolvedValue({});

    mount();

    await screen.findByText("No engines configured");
    expect(screen.queryByRole("region", { name: "Left out" })).toBeNull();
  });

  it("edits a record that parses: the editor holds the shown record with its markers, and saving sends what was edited", async () => {
    const fix = vi
      .spyOn(api.services, "fixLeftOut")
      .mockResolvedValue({ ...registry([unparsed]), services: {} });
    vi.spyOn(api.services, "registry").mockResolvedValue(registry([parsed, unparsed]));
    vi.spyOn(api, "status").mockResolvedValue({});
    mount();
    const list = await screen.findByRole("region", { name: "Left out" });

    await userEvent.click(within(list).getByRole("button", { name: "Edit held-engine" }));

    const dialog = await screen.findByRole("dialog");
    const editor = within(dialog).getByRole("textbox", { name: /record/i }) as HTMLTextAreaElement;
    const shown = JSON.parse(editor.value);
    expect(shown.config.headers).toEqual([{ id: "ent_0123456789abcdef", name: "X-Vendor", configured: true }]); // an entry, never a value
    expect(within(dialog).getByText(/secret entry with an id/i)).toBeTruthy();
    shown.config.models = [];
    fireEvent.change(editor, { target: { value: JSON.stringify(shown) } });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save record" }));

    await waitFor(() => expect(fix).toHaveBeenCalledTimes(1));
    expect(fix).toHaveBeenCalledWith("services", "held-engine", shown);
    await waitFor(() => expect(screen.queryByText("held-engine")).toBeNull());
    expect(screen.getByText("junk-frontend")).toBeTruthy();
  });

  it("does not send an edit that is not JSON, and says so", async () => {
    const fix = vi.spyOn(api.services, "fixLeftOut");
    vi.spyOn(api.services, "registry").mockResolvedValue(registry([parsed]));
    vi.spyOn(api, "status").mockResolvedValue({});
    mount();
    const list = await screen.findByRole("region", { name: "Left out" });
    await userEvent.click(within(list).getByRole("button", { name: "Edit held-engine" }));
    const dialog = await screen.findByRole("dialog");

    fireEvent.change(within(dialog).getByRole("textbox", { name: /record/i }), { target: { value: "{not json" } });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save record" }));

    expect(await within(dialog).findByRole("alert")).toBeTruthy();
    expect(fix).not.toHaveBeenCalled();
  });

  it("shows the reason the server refused an edit and keeps the editor open", async () => {
    vi.spyOn(api.services, "fixLeftOut").mockRejectedValue(
      new ApiError("invalid_service_registry", "service 'held-engine' models must be a list", 400),
    );
    vi.spyOn(api.services, "registry").mockResolvedValue(registry([parsed]));
    vi.spyOn(api, "status").mockResolvedValue({});
    mount();
    const list = await screen.findByRole("region", { name: "Left out" });
    await userEvent.click(within(list).getByRole("button", { name: "Edit held-engine" }));
    const dialog = await screen.findByRole("dialog");

    await userEvent.click(within(dialog).getByRole("button", { name: "Save record" }));

    expect(await within(dialog).findByText(/models must be a list/)).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("for a record that cannot be shown, names the fields that could not be read and starts the editor empty", async () => {
    vi.spyOn(api.services, "registry").mockResolvedValue(registry([unparsed]));
    vi.spyOn(api, "status").mockResolvedValue({});
    mount();
    const list = await screen.findByRole("region", { name: "Left out" });

    await userEvent.click(within(list).getByRole("button", { name: "Edit junk-frontend" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/cannot be shown/i)).toBeTruthy();
    expect(within(dialog).getByText("config")).toBeTruthy();
    expect((within(dialog).getByRole("textbox", { name: /record/i }) as HTMLTextAreaElement).value).toBe("{}");
  });

  it("removes a record after the operator confirms it by name", async () => {
    const remove = vi
      .spyOn(api.services, "removeLeftOut")
      .mockResolvedValue(registry([unparsed]));
    vi.spyOn(api.services, "registry").mockResolvedValue(registry([parsed, unparsed]));
    vi.spyOn(api, "status").mockResolvedValue({});
    mount();
    const list = await screen.findByRole("region", { name: "Left out" });

    await userEvent.click(within(list).getByRole("button", { name: "Remove held-engine" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByRole("textbox"), "held-engine");
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove record" }));

    await waitFor(() => expect(remove).toHaveBeenCalledWith("services", "held-engine"));
    await waitFor(() => expect(screen.queryByText("held-engine")).toBeNull());
  });
});
