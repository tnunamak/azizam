import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ProvidersPage from "@/app/providers/page";
import ShowcasePage from "@/app/showcase/page";
import { canonicalDestinationPath } from "@/lib/product";

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("integrated UI coherence", () => {
  it("treats /ui/home as the Home destination", () => {
    expect(canonicalDestinationPath("/")).toBe("/");
    expect(canonicalDestinationPath("/ui/")).toBe("/");
    expect(canonicalDestinationPath("/ui/home/")).toBe("/");
    expect(canonicalDestinationPath("/home")).toBe("/");
    expect(canonicalDestinationPath("/ui/models/")).toBe("/models");
  });

  it("announces Showcase recorded-example loading as a status", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    renderWithQuery(<ShowcasePage />);
    const loading = screen.getByRole("status", {
      name: "Loading recorded examples",
    });
    expect(loading).toHaveAttribute("aria-busy", "true");
  });

  it("feature-detects a missing provider API before showing provider controls", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 404 })),
    );
    renderWithQuery(<ProvidersPage />);
    expect(
      await screen.findByText("Provider controls are not installed"),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("renders normalized provider status without secret fields", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          providers: [
            {
              id: "openrouter",
              name: "OpenRouter",
              enabled: true,
              reachable: true,
              key_set: true,
              spend_usd: 1.25,
              cap_usd: 10,
              secret_ref: "OPENROUTER_API_KEY",
            },
          ],
        }),
      ),
    );
    renderWithQuery(<ProvidersPage />);
    expect(await screen.findByRole("heading", { name: "OpenRouter" })).toBeVisible();
    expect(screen.getByText("Ready")).toBeVisible();
    expect(screen.getByText("Set")).toBeVisible();
    expect(screen.getByText("$1.25 of $10.00")).toBeVisible();
    await waitFor(() =>
      expect(screen.queryByText("OPENROUTER_API_KEY")).toBeNull(),
    );
  });

  it("does not call unknown or disabled providers Starting", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          providers: [
            {
              id: "disabled-remote",
              name: "Disabled remote",
              enabled: false,
              reachable: null,
              key_set: null,
            },
          ],
        }),
      ),
    );
    renderWithQuery(<ProvidersPage />);
    expect(
      await screen.findByRole("heading", { name: "Disabled remote" }),
    ).toBeVisible();
    expect(screen.getByText("Unavailable")).toBeVisible();
    expect(screen.getByText("Provider is disabled.")).toBeVisible();
    expect(screen.queryByText("Starting")).toBeNull();
  });
});
