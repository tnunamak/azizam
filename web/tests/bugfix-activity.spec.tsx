import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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
      expect(screen.getByRole("table", { name: "Request receipts" })).toHaveAttribute("data-dense");
      const table = screen.getByRole("table", { name: "Request receipts" });
      expect(
        within(table).getByRole("button", { name: /Request details:/ }).closest("tr"),
      ).toBeVisible();
      fireEvent.click(screen.getByRole("button", { name: /Request details:/ }));
      expect(screen.getByRole("dialog")).toHaveTextContent(label);
      expect(screen.getByRole("dialog")).toHaveTextContent("Owner");
      fireEvent.click(screen.getByText("Technical details"));
      expect(
        screen.getByLabelText("Technical request details"),
      ).toHaveTextContent('"status_code": 529');
      client.clear();
    },
  );
});

describe("model names on Activity requests", () => {
  it("names a llama.cpp weight file as the model, never the file", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        const body = url.includes("/me/api/models/catalog")
          ? { aliases: [{ id: "default", title: "Default" }], models: [] }
          : url.includes("/me/api/home")
            ? { capabilities: [] }
            : {
                records: [
                  {
                    receipt: {
                      request_id: "req_gguf",
                      intent: "chat.completions",
                      status: "ok",
                      requested_model: "default",
                      resolved_model:
                        "/media/models/Qwen3.8-27B-Uncensored-Q4_K_M.gguf",
                    },
                    person_label: "Owner",
                    app_label: "t3-pi-agent",
                    created_at: 1700000000,
                  },
                ],
                next_cursor: null,
                people: [],
              };
        return { ok: true, json: async () => body };
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
    expect(
      await screen.findByText(/Qwen3\.8 27B Uncensored/),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Request details:/ }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Name used");
    expect(dialog).toHaveTextContent("Qwen3.8 27B Uncensored");
    // Outside the raw technical JSON, no weight file name anywhere.
    const page = document.body.cloneNode(true) as HTMLElement;
    page.querySelectorAll("details").forEach((node) => node.remove());
    expect(page.innerHTML).not.toMatch(/\.gguf|Q4_K_M/);
    client.clear();
  });
});
