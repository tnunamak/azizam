import { afterEach, expect, it, vi } from "vitest";
import {
  cleanup,
  render,
  screen,
  within,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import Activity from "./page";

vi.mock("./requests", () => ({ Requests: () => null }));
const pointerMethods = [
  "hasPointerCapture",
  "setPointerCapture",
  "releasePointerCapture",
  "scrollIntoView",
] as const;
for (const method of pointerMethods)
  Object.defineProperty(HTMLElement.prototype, method, {
    configurable: true,
    value: () => false,
  });
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
function mount() {
  vi.spyOn(api.users, "list").mockResolvedValue([
    {
      id: "person-beyond-audit-window",
      issuer: "",
      subject: "",
      display_name: "Azizam Owner",
      name: null,
      email: null,
      created_at: 0,
      last_login_at: null,
      roles: [],
    },
  ]);
  const audit = vi
    .spyOn(api.audit, "list")
    .mockResolvedValue({ records: [], next_cursor: null });
  render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false, gcTime: 0 } },
        })
      }
    >
      <Activity />
    </QueryClientProvider>,
  );
  return audit;
}
it("offers human names from the people source beyond the recent audit window", async () => {
  const user = userEvent.setup();
  const audit = mount();
  await screen.findByText("No changes yet");
  await user.click(screen.getByRole("combobox", { name: "Person" }));
  await user.click(await screen.findByRole("option", { name: "Azizam Owner" }));
  await waitFor(() =>
    expect(audit).toHaveBeenCalledWith(
      expect.objectContaining({ actor_id: "person-beyond-audit-window" }),
    ),
  );
  expect(screen.getByText("Technical: filter by person ID")).toBeVisible();
  expect(screen.queryByText("Find a person")).toBeNull();
});
it.each([0, 1])(
  "clears the category, applied actor and draft with Clear filters button %s",
  async (buttonIndex) => {
    const user = userEvent.setup();
    const audit = mount();
    await screen.findByText("No changes yet");
    await user.click(screen.getByRole("combobox", { name: "Change type" }));
    await user.click(screen.getByRole("option", { name: "Apps" }));
    await user.click(screen.getByText("Technical: filter by person ID"));
    await user.type(
      screen.getByRole("textbox", { name: "Technical person ID" }),
      "opaque-person",
    );
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await screen.findByText("No changes match these filters");
    await user.click(
      screen.getAllByRole("button", { name: "Clear filters" })[buttonIndex],
    );
    expect(
      screen.getByRole("textbox", { name: "Technical person ID" }),
    ).toHaveValue("");
    expect(
      within(screen.getByRole("combobox", { name: "Change type" })).getByText(
        "All changes",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await screen.findByText("No changes yet");
    await waitFor(() =>
      expect(audit).toHaveBeenLastCalledWith(
        expect.objectContaining({
          actor_id: undefined,
          action_prefix: undefined,
        }),
      ),
    );
  },
);
it("shows people lookup errors with a retry action", async () => {
  mount();
  vi.mocked(api.users.list).mockRejectedValue(new Error("offline"));
  cleanup();
  render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false, gcTime: 0 } },
        })
      }
    >
      <Activity />
    </QueryClientProvider>,
  );
  expect(
    await screen.findByText(
      "People could not be loaded. Recent activity names are still available.",
    ),
  ).toBeVisible();
});
