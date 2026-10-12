import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import WorkflowsPage from "@/app/workflows/page";
import { ViewerContext, type Viewer } from "@/components/shell/session";

const ownerViewer: Viewer = {
  authenticated: true,
  name: "Tim",
  email: null,
  role: "owner",
  admin: true,
};

const catalog = {
  schema_version: 1,
  workflows: [
    {
      id: "image.product-render",
      capability: "image",
      title: "Product render",
      description: "Create product renders.",
      technical_notes:
        "Create a clean product render with the local image workflow. This full manifest note stays in technical details so browsing cards remain compact.",
      inputs: [
        {
          name: "prompt",
          type: "string",
          required: true,
          accepted: true,
          description: "What to render.",
        },
        {
          name: "seed",
          type: "integer",
          default: 0,
          minimum: 0,
          maximum: 65_535,
          accepted: true,
        },
        {
          name: "style_preset",
          type: "string",
          default: "studio",
          enum: ["studio", "flatlay"],
          accepted: false,
        },
      ],
      outputs: [
        {
          name: "render",
          media_type: "image/png",
          description: "Final product render asset.",
        },
      ],
      engine_binding: "image-local",
      availability: {
        status: "ready",
        reason: "Configured for this access plan.",
        available: true,
      },
      typical_seconds: 12,
      aliases: ["image", "product-shot"],
      examples: {
        curl: "curl example",
        python: "python example",
        javascript: "js example",
      },
      native_route: "/v1/workflows/image.product-render/run",
      discovery_url: "/gateway/v1/workflows/image.product-render",
      try_url: "/try/?workflow=image.product-render",
    },
  ],
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("does not reopen a removed workflow when a later catalog restores the same ID", async () => {
  let present = true;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).includes("/me/api/media")) return Response.json({ assets: [] });
    return Response.json({ ...catalog, workflows: present ? catalog.workflows : [] });
  }));
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider value={ownerViewer}>
        <WorkflowsPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Details for Product render" }));
  expect(screen.getByRole("dialog", { name: "Product render" })).toBeVisible();

  present = false;
  await act(async () => {
    await client.refetchQueries({ queryKey: ["workflows", "catalog"] });
  });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(await screen.findByText("No workflows are available.")).toBeVisible();

  present = true;
  await act(async () => {
    await client.refetchQueries({ queryKey: ["workflows", "catalog"] });
  });
  await screen.findByRole("listitem", { name: "Product render", hidden: true });
  expect(screen.queryByRole("dialog")).toBeNull();
  await user.click(screen.getByRole("button", { name: "Details for Product render" }));
  expect(screen.getByRole("dialog", { name: "Product render" })).toBeVisible();
  view.unmount();
  client.clear();
});
