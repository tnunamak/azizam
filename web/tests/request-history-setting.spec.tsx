import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RequestHistorySetting } from "@/app/apps/request-history-setting";
import { accountApi, type AccountApp } from "@/lib/account-api";

vi.mock("@/lib/account-api", () => ({
  accountApi: { setRequestHistory: vi.fn() },
}));

const shared: AccountApp = {
  id: "app_voice",
  name: "Kitchen voice",
  kind: "service",
  disabled_at: null,
  created_at: 1,
  access_plan: null,
  can_mint_keys: true,
  credentials: [],
  request_history: {
    keep: false,
    retention: "7_days",
    manager_name: "Tim Nunamaker",
  },
} as AccountApp;

function renderSetting(app: AccountApp = shared, onSaved = vi.fn()) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <RequestHistorySetting app={app} onSaved={onSaved} />
    </QueryClientProvider>,
  );
  return { onSaved };
}

beforeEach(() => {
  // Radix Select measures pointer targets that jsdom does not implement.
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.scrollIntoView ??= () => {};
  vi.mocked(accountApi.setRequestHistory).mockReset();
});
afterEach(cleanup);

it("shows the switch off and says nothing is kept until it is turned on", () => {
  renderSetting();

  expect(
    screen.getByRole("switch", { name: "Keep request history" }),
  ).not.toBeChecked();
  expect(
    screen.getByText("Requests from this app are not kept."),
  ).toBeVisible();
});

it("turning it on saves the choice and names the person who can read the history", async () => {
  const saved = {
    ...shared,
    request_history: {
      keep: true,
      retention: "7_days",
      manager_name: "Tim Nunamaker",
    },
  };
  vi.mocked(accountApi.setRequestHistory).mockResolvedValue(
    saved as AccountApp,
  );
  const { onSaved } = renderSetting();

  await userEvent.click(
    screen.getByRole("switch", { name: "Keep request history" }),
  );

  await waitFor(() =>
    expect(accountApi.setRequestHistory).toHaveBeenCalledWith("app_voice", {
      keep: true,
      retention: "7_days",
    }),
  );
  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(saved));
  expect(
    screen.getByText(
      "Requests from this app are kept for 7 days. Tim Nunamaker can read them in Request history.",
    ),
  ).toBeVisible();
});

it("picks a period from the offered choices, never free text", async () => {
  vi.mocked(accountApi.setRequestHistory).mockResolvedValue(shared);
  renderSetting({
    ...shared,
    request_history: {
      keep: true,
      retention: "7_days",
      manager_name: "Tim Nunamaker",
    },
  });

  await userEvent.click(
    screen.getByRole("combobox", { name: "Keep request history for" }),
  );
  expect(
    screen.getAllByRole("option").map((option) => option.textContent),
  ).toEqual(["1 day", "7 days", "30 days"]);
  await userEvent.click(screen.getByRole("option", { name: "30 days" }));

  await waitFor(() =>
    expect(accountApi.setRequestHistory).toHaveBeenCalledWith("app_voice", {
      keep: true,
      retention: "30_days",
    }),
  );
});

it("says why the change did not save and keeps the switch where it was", async () => {
  vi.mocked(accountApi.setRequestHistory).mockRejectedValue(new Error("nope"));
  renderSetting();

  await userEvent.click(
    screen.getByRole("switch", { name: "Keep request history" }),
  );

  expect(
    await screen.findByText(
      "Could not save the request history setting. Try again.",
    ),
  ).toBeVisible();
  expect(
    screen.getByRole("switch", { name: "Keep request history" }),
  ).not.toBeChecked();
});

it("shows nothing for an app the gateway gives no history setting (a personal app)", () => {
  const { container } = render(
    <QueryClientProvider client={new QueryClient()}>
      <RequestHistorySetting
        app={{ ...shared, kind: "personal", request_history: undefined }}
        onSaved={vi.fn()}
      />
    </QueryClientProvider>,
  );

  expect(container).toBeEmptyDOMElement();
});

it("names the manager by role when the manager has no name", () => {
  renderSetting({
    ...shared,
    request_history: { keep: true, retention: "7_days", manager_name: "" },
  });

  expect(
    screen.getByText(
      "Requests from this app are kept for 7 days. The person who manages this App can read them in Request history.",
    ),
  ).toBeVisible();
});
