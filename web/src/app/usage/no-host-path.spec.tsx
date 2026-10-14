import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { ViewerContext } from "@/components/shell/session";
import {
  GGUF,
  HOST_PATH,
  expectNoHostPath,
  readableText,
} from "../../../tests/helpers/host-path";
import UsagePage from "./page";

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <>{children}</>,
  Bar: () => null,
  BarChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CartesianGrid: () => null,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}));
HTMLElement.prototype.scrollIntoView = vi.fn();
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("Usage shows no host path in the table, receipts or details", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) =>
      Promise.resolve(
        Response.json(
          String(input).includes("/usage/summary")
            ? {
                requests: 2,
                media_count: 0,
                by_model: [{ model: GGUF, capability: "chat", requests: 2 }],
                by_day: [{ date: "2026-10-01", chat: 2 }],
                receipts: [
                  {
                    request_id: "r1",
                    capability: "chat",
                    requested_model: "default",
                    resolved_model: GGUF,
                    status: "succeeded",
                    accepted_at_ms: 1790870400000,
                  },
                ],
              }
            : {},
        ),
      ),
    ),
  );
  const user = userEvent.setup();
  render(
    <ViewerContext.Provider
      value={{ authenticated: true, name: "Owner", email: null, role: "owner", admin: true }}
    >
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <UsagePage />
      </QueryClientProvider>
    </ViewerContext.Provider>,
  );
  expect(await screen.findByText("Request counts and recorded spend this month, busiest models first")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "View recent requests" }));
  expectNoHostPath(expect, "usage");
  await user.click(
    await screen.findByRole("button", { name: /Receipt details for Chat request/ }),
  );
  expectNoHostPath(expect, "usage receipt");
  // The owner's technical details keep the exact id for routing diagnostics.
  // Everything outside that disclosure stays free of the path.
  await user.click(screen.getByText("Technical details", { exact: true }));
  const details = screen.getByText("Technical details").closest("details")!;
  expect(details).toHaveTextContent("/GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf");
  const outside = document.body.cloneNode(true) as HTMLElement;
  outside.querySelectorAll("details").forEach((node) => node.remove());
  expect(readableText(outside), "usage receipt: outside technical details").not.toMatch(
    HOST_PATH,
  );
});
