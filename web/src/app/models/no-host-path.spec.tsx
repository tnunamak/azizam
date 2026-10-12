import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { ViewerContext } from "@/components/shell/session";
import { GGUF, expectNoHostPath } from "../../../tests/helpers/host-path";
import ModelsClient from "./models-client";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("Models shows no host path when a model is registered by file path", async () => {
  // The real productApi.models runs here: it is what shortens the path.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json({
        schema_version: 1,
        object: "models.catalog",
        aliases: [],
        models: [
          {
            id: GGUF,
            object: "model_catalog.model",
            display_name: GGUF,
            name: GGUF,
            alias: false,
            operations: ["chat.completions"],
            resolves_to: {},
            description: null,
            capabilities: [],
            provider: null,
            engine_label: "llama.cpp",
            locality: "local",
            cost: { kind: "free", price: null },
            timing: {},
            availability: { available: true, status: "ready" },
            io: { input_modalities: ["text"], output_modalities: ["text"] },
            examples: [],
            recent_receipts: [],
            use: { model: GGUF },
            type: "language",
          },
        ],
        counts: { aliases: 0, models: 1, capabilities: 0 },
      }),
    ),
  );
  render(
    <ViewerContext.Provider
      value={{ authenticated: true, name: "Owner", email: null, role: "owner", admin: true }}
    >
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ModelsClient />
      </QueryClientProvider>
    </ViewerContext.Provider>,
  );
  await userEvent.setup().click(await screen.findByRole("button", { name: "Show all models" }));
  // The model is on screen by its file name, so the check below is not vacuous.
  expect((await screen.findAllByText(/Qwen3\.8/)).length).toBeGreaterThan(0);
  expectNoHostPath(expect, "models");
});
