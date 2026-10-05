import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import WorkflowsPage from "@/app/workflows/page";

const catalog = {
  schema_version: 1,
  workflows: [
    {
      id: "image.product-render",
      capability: "image",
      title: "Product render",
      description:
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
      try_url: "/ui/try/?workflow=image.product-render",
    },
  ],
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderWorkflows() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/me/api/media")) {
        return Response.json({ assets: [], models: [], next_cursor: null });
      }
      return Response.json(catalog);
    }),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <WorkflowsPage />
    </QueryClientProvider>,
  );
}

it("keeps browsing cards compact and leaves manifest facts in the detail view", async () => {
  renderWorkflows();
  const card = within(
    await screen.findByRole("article", { name: "Product render" }),
  );

  expect(card.getByRole("img", { name: "Text input" })).toBeVisible();
  expect(card.queryByRole("img", { name: "Number input" })).toBeNull();
  expect(card.getByText(/Create a clean product render/)).toBeVisible();
  expect(card.queryByText("Output")).toBeNull();
  expect(card.queryByText("image/png")).toBeNull();
  expect(card.queryByText("seed: 0 to 65535")).toBeNull();
  expect(card.queryByText(/full manifest note/)).toBeNull();
  expect(card.queryByText("studio, flatlay")).toBeNull();
});

it("puts manifest details behind a closed native disclosure", async () => {
  const user = userEvent.setup();
  renderWorkflows();
  await user.click(await screen.findByRole("button", { name: "Details" }));
  const sheet = within(screen.getByRole("dialog", { name: "Product render" }));
  const disclosure = sheet
    .getByText("Technical details")
    .closest("details") as HTMLDetailsElement;

  expect(disclosure.open).toBe(false);
  expect(
    sheet.queryByText(/full manifest note stays in technical details/),
  ).not.toBeVisible();

  await user.click(sheet.getByText("Technical details"));

  expect(disclosure.open).toBe(true);
  expect(
    sheet.getByText(/full manifest note stays in technical details/),
  ).toBeVisible();
  expect(sheet.getByText("image.product-render")).toBeVisible();
  expect(sheet.getByText("image-local")).toBeVisible();
  expect(sheet.getByText("~12 s")).toBeVisible();
  expect(
    sheet.getByText("/v1/workflows/image.product-render/run"),
  ).toBeVisible();
  expect(sheet.getByText("product-shot")).toBeVisible();
});

it("renders mobile input summaries beside the desktop table", async () => {
  const user = userEvent.setup();
  renderWorkflows();
  await user.click(await screen.findByRole("button", { name: "Details" }));
  const sheet = within(screen.getByRole("dialog", { name: "Product render" }));
  const mobileSummaries = sheet.getByRole("list", { name: "Input summaries" });
  const tableScrollport = sheet.getByLabelText("Scrollable inputs");

  expect(mobileSummaries).toHaveClass("sm:hidden");
  expect(tableScrollport).toHaveClass("sm:block");
  expect(within(mobileSummaries).getByText("prompt")).toBeVisible();
  expect(within(mobileSummaries).getByText("string · Required")).toBeVisible();
  expect(within(mobileSummaries).getByText("seed")).toBeVisible();
  expect(within(mobileSummaries).getByText("0 to 65535")).toBeVisible();
  expect(sheet.getByRole("columnheader", { name: "Range" })).toBeVisible();
  expect(sheet.getByRole("cell", { name: "studio, flatlay" })).toBeVisible();
});
