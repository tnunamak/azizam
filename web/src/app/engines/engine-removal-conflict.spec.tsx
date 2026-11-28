import { afterEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, ApiError, type Service, type ServiceRegistry } from "@/lib/api";
import Page from "./page";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ admin: true }),
}));

const service = (id: string, role = "primary", enabled = true): Service => ({
  id,
  name: id,
  kind: "http",
  source: "ui",
  enabled,
  editable: true,
  capabilities: ["chat"],
  role,
  config: { base_url: "http://engine-a.test" },
});
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

describe("engine removal conflict", () => {
it("retains a conflicted engine removal and retries only the confirmed target", async () => {
  const { toast } = await import("@/lib/toast");
  const notice = vi.spyOn(toast, "error");
  const initial: ServiceRegistry = {
    schema_version: 1,
    services: { primary: service("primary") },
  };
  const empty: ServiceRegistry = { schema_version: 1, services: {} };
  vi.spyOn(api.services, "registry").mockResolvedValue(initial);
  vi.spyOn(api, "status").mockResolvedValue({});
  let rejectFirst!: (reason: unknown) => void;
  const remove = vi.spyOn(api.services, "remove")
    .mockImplementationOnce(() => new Promise<ServiceRegistry>((_resolve, reject) => {
      rejectFirst = reject;
    }))
    .mockResolvedValue(empty);
  const user = userEvent.setup();
  mount();
  const actions = await screen.findByRole("button", { name: "Actions for primary" });
  await user.click(actions);
  await user.click(screen.getByRole("menuitem", { name: "Remove engine" }));
  const dialog = screen.getByRole("dialog", { name: "Remove engine?" });
  await user.type(within(dialog).getByRole("textbox"), "primary");
  const confirm = within(dialog).getByRole("button", { name: "Remove engine" });
  await user.click(confirm);
  await waitFor(() => expect(remove).toHaveBeenCalledOnce());
  expect(confirm).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled();
  await user.keyboard("{Escape}");
  await user.click(confirm);
  expect(dialog).toBeVisible();
  expect(remove).toHaveBeenCalledOnce();

  await act(async () => rejectFirst(new ApiError("revision_mismatch", "Old revision", 412)));
  await waitFor(() => expect(confirm).toBeEnabled());
  expect(notice).toHaveBeenCalledWith("Engine could not be removed", {
    description: "Engine settings changed elsewhere. Refresh and try again.",
  });
  expect(dialog).toBeVisible();
  expect(within(dialog).getByRole("textbox")).toHaveValue("primary");
  expect(actions.isConnected).toBe(true);
  expect(remove).toHaveBeenCalledOnce();

  await user.click(confirm);
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(remove.mock.calls).toEqual([["primary"], ["primary"]]);
  expect(screen.queryByRole("button", { name: "Actions for primary" })).toBeNull();
});
});
