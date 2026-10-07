import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import TryPage from "@/app/try/page";

const account = vi.hoisted(() => ({
  session: vi.fn(),
  mintKey: vi.fn(),
  revokeKey: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/account-api", () => ({ accountApi: account }));

const models = {
  aliases: [],
  models: [
    {
      id: "chat",
      name: "Chat model",
      capabilities: ["chat"],
      locality: "local",
    },
  ],
};
const workflows = {
  workflows: [
    { id: "workflow", title: "Working workflow", inputs: [], outputs: [] },
  ],
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
const fetchMock = vi.fn();

beforeEach(() => {
  account.session.mockResolvedValue({
    authenticated: true,
    apps: [{ id: "try", name: "Try" }],
  });
  account.mintKey.mockResolvedValue({ id: "try-key", api_key: "test-key" });
  fetchMock
    .mockReset()
    .mockImplementation(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST")
        return json({ error: { message: "Request failed" } }, 500);
      return json(url.includes("workflows") ? workflows : models);
    });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

it("generic result retry reloads catalogs without repeating generation", async () => {
  const user = userEvent.setup();
  render(<TryPage />);
  await waitFor(() =>
    expect(screen.getByRole("combobox")).toHaveTextContent("Chat model"),
  );
  await user.click(screen.getByRole("button", { name: "Run" }));
  expect(await screen.findByText("Request failed")).toBeVisible();
  const reads = fetchMock.mock.calls.filter(
    ([, init]) => init?.method !== "POST",
  ).length;
  await user.click(screen.getByRole("button", { name: "Try again" }));
  expect(
    fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
  ).toHaveLength(1);
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method !== "POST"),
    ).toHaveLength(reads + 2),
  );
  expect(
    fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
  ).toHaveLength(1);
  expect(screen.getByText("Request failed")).toBeVisible();
  expect(account.mintKey).toHaveBeenCalledTimes(1);
  expect(account.revokeKey).not.toHaveBeenCalled();
});

it.each(["model", "workflow"])(
  "%s metadata retry only reloads the failed catalog",
  async (section) => {
    let failing = true;
    fetchMock.mockImplementation(async (url: string) => {
      const isFailedSection =
        section === "workflow"
          ? url.includes("workflows")
          : url.includes("models");
      if (failing && isFailedSection) return json({}, 500);
      return json(url.includes("workflows") ? workflows : models);
    });
    const user = userEvent.setup();
    render(<TryPage />);
    if (section === "workflow")
      await user.click(screen.getByRole("tab", { name: "Workflow" }));
    const retry = await screen.findByRole("button", {
      name: `Retry ${section} catalog`,
    });
    failing = false;
    fetchMock.mockClear();
    await user.click(retry);
    await waitFor(() =>
      expect(screen.getByRole("combobox")).toHaveTextContent(
        section === "workflow" ? "Working workflow" : "Chat model",
      ),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1]?.method).not.toBe("POST");
    expect(account.mintKey).not.toHaveBeenCalled();
  },
);
