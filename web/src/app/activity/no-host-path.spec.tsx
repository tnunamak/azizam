import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { GGUF, expectNoHostPath } from "../../../tests/helpers/host-path";
import { Requests } from "./requests";

HTMLElement.prototype.scrollIntoView = vi.fn();
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("Activity shows no host path in rows or details", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        records: [
          {
            receipt: {
              request_id: "req_path",
              intent: "chat.completions",
              requested_model: "default",
              resolved_model: GGUF,
              status: "succeeded",
              latency_ms: 120,
            },
            person_label: "Owner",
            app_label: "Try",
            created_at: 1_790_870_400,
          },
        ],
        next_cursor: null,
        people: [],
        sources: [],
      }),
    }),
  );
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}
    >
      <Requests />
    </QueryClientProvider>,
  );
  const row = await screen.findByRole("button", { name: /Request details:/ });
  expectNoHostPath(expect, "activity");
  await userEvent.setup().click(row);
  expect(await screen.findByRole("dialog")).toBeVisible();
  expectNoHostPath(expect, "activity details");
});
