import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Requests } from "@/app/activity/requests";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("recorded decision outcomes", () => {
  it.each([
    ["error", "Failed"],
    ["rejected", "Rejected"],
    ["failed", "Failed"],
    ["ok", "Completed"],
    ["succeeded", "Completed"],
    ["in_progress", "In progress"],
    ["stale_incomplete", "Outcome not recorded"],
  ])(
    "renders %s honestly in the row and receipt detail",
    async (status, label) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: true,
          json: async () => ({
            records: [
              {
                receipt: {
                  request_id: "req_failure",
                  intent: "decision.systemone",
                  status,
                  metadata: { status_code: 529 },
                },
                person_label: "Owner",
                app_label: "Try",
                created_at: 1700000000,
              },
            ],
            next_cursor: null,
            people: [],
          }),
        }),
      );
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      });
      render(
        <QueryClientProvider client={client}>
          <Requests />
        </QueryClientProvider>,
      );
      expect(await screen.findByText(label)).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: /Request details:/ }));
      expect(screen.getByRole("dialog")).toHaveTextContent(`${label} · Owner`);
      fireEvent.click(screen.getByText("Technical details"));
      expect(
        screen.getByLabelText("Technical request details"),
      ).toHaveTextContent('"status_code": 529');
      client.clear();
    },
  );
});
