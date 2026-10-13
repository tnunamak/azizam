import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { ViewerContext } from "@/components/shell/session";
import { expectNoHostPath } from "../../../tests/helpers/host-path";
import ModelsClient from "./models-client";

const MODEL_PATH = "/opt/models/model-b.gguf";

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
            id: MODEL_PATH,
            object: "model_catalog.model",
            display_name: MODEL_PATH,
            name: MODEL_PATH,
            alias: false,
            operations: ["chat.completions"],
            resolves_to: {},
            description: null,
            capabilities: [],
            provider: null,
            engine_label: "engine-a",
            locality: "local",
            cost: { kind: "free", price: null },
            timing: {},
            availability: { available: true, status: "ready" },
            io: { input_modalities: ["text"], output_modalities: ["text"] },
            examples: [],
            recent_receipts: [],
            use: { model: MODEL_PATH },
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
  expect((await screen.findAllByText(/model-b\.gguf/)).length).toBeGreaterThan(0);
  expect(document.body.textContent).not.toContain("/opt/models/");
  expectNoHostPath(expect, "models");
});
