import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RequestHistorySetting } from "@/app/apps/request-history-setting";
import { accountApi, type AccountApp } from "@/lib/account-api";
import type { RequestHistoryChoice } from "@/lib/request-history";

vi.mock("@/lib/account-api", () => ({
  accountApi: { setRequestHistory: vi.fn(), requestHistory: vi.fn() },
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

// The gateway's answer to GET /me/api/retention. The choices and the household state come from here.
function policy(
  overrides: Partial<RequestHistoryChoice> = {},
): RequestHistoryChoice {
  return {
    keep: true,
    retention: "7_days",
    parts: ["accepted", "received"],
    choices: ["1_day", "7_days", "30_days"],
    allowed_parts: ["accepted", "received"],
    household_off: false,
    notice_due: false,
    ...overrides,
  };
}

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
  vi.mocked(accountApi.requestHistory).mockReset();
  vi.mocked(accountApi.requestHistory).mockResolvedValue(policy());
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

it("says what is kept, for how long at most, who reads it, and that household limits can shorten it", async () => {
  renderSetting({
    ...shared,
    request_history: {
      keep: true,
      retention: "7_days",
      manager_name: "Tim Nunamaker",
    },
  });

  expect(
    await screen.findByText(
      "Requests and replies from this app are kept for up to 7 days. Only Tim Nunamaker, who manages this app, can read them in Request history. The household's limits can shorten this or stop it.",
    ),
  ).toBeVisible();
});

it("names the manager by role when the manager has no name", async () => {
  renderSetting({
    ...shared,
    request_history: { keep: true, retention: "7_days", manager_name: "" },
  });

  expect(
    await screen.findByText(
      "Requests and replies from this app are kept for up to 7 days. Only the person who manages this app can read them in Request history. The household's limits can shorten this or stop it.",
    ),
  ).toBeVisible();
});

it("says nothing is kept, and offers no switch, when the household has request history off", async () => {
  vi.mocked(accountApi.requestHistory).mockResolvedValue(
    policy({ household_off: true, keep: false }),
  );
  renderSetting();

  expect(
    await screen.findByText("Off for the household: nothing new is kept."),
  ).toBeVisible();
  expect(
    screen.getByRole("switch", { name: "Keep request history" }),
  ).toBeDisabled();
});

it("turning it on saves the choice and shows the copy for the saved app", async () => {
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
    await screen.findByText(
      "Requests and replies from this app are kept for up to 7 days. Only Tim Nunamaker, who manages this app, can read them in Request history. The household's limits can shorten this or stop it.",
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

  const period = await screen.findByRole("combobox", {
    name: "Keep request history for",
  });
  await waitFor(() => expect(period).toBeEnabled());
  await userEvent.click(period);
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

it("offers only the periods the gateway says it offers", async () => {
  vi.mocked(accountApi.requestHistory).mockResolvedValue(
    policy({ choices: ["1_day", "30_days"] }),
  );
  renderSetting({
    ...shared,
    request_history: {
      keep: true,
      retention: "1_day",
      manager_name: "Tim Nunamaker",
    },
  });

  const period = await screen.findByRole("combobox", {
    name: "Keep request history for",
  });
  await waitFor(() => expect(period).toBeEnabled());
  await userEvent.click(period);
  expect(
    screen.getAllByRole("option").map((option) => option.textContent),
  ).toEqual(["1 day", "30 days"]);
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
