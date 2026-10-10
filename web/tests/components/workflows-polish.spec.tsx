import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, within } from "@testing-library/react";
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
      try_url: "/ui/try/?workflow=image.product-render",
    },
  ],
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderWorkflows(viewer?: Viewer) {
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
  const page = (
    <QueryClientProvider client={client}>
      <WorkflowsPage />
    </QueryClientProvider>
  );
  render(
    viewer ? (
      <ViewerContext.Provider value={viewer}>{page}</ViewerContext.Provider>
    ) : (
      page
    ),
  );
}

it("keeps browsing rows compact and leaves manifest facts in the detail view", async () => {
  const user = userEvent.setup();
  renderWorkflows();
  const card = within(
    await screen.findByRole("listitem", { name: "Product render" }),
  );

  expect(card.queryByText("Needs: text")).toBeNull();
  expect(card.queryByText("Optional: a number")).toBeNull();
  expect(card.getByText("From text → a picture")).toBeVisible();
  expect(card.queryByRole("img", { name: "Text input" })).toBeNull();
  expect(card.queryByText("prompt")).toBeNull();
  expect(card.queryByText("Create product renders.")).toBeNull();
  expect(card.queryByText("Output")).toBeNull();
  expect(card.queryByText("image/png")).toBeNull();
  expect(card.queryByText("seed: 0 to 65535")).toBeNull();
  expect(card.queryByText(/full manifest note/)).toBeNull();
  expect(card.queryByText("studio, flatlay")).toBeNull();
  await user.click(card.getByRole("button", { name: "Details for Product render" }));
  const sheet = within(screen.getByRole("dialog", { name: "Product render" }));
  expect(sheet.getByText("Needs: text")).toBeVisible();
  expect(sheet.getByText("Optional: a number")).toBeVisible();
  expect(sheet.getByText("Create product renders.")).toBeVisible();
});

it("keeps the manifest's research and engine notes from members", async () => {
  const user = userEvent.setup();
  renderWorkflows();
  await user.click(await screen.findByRole("button", { name: "Details for Product render" }));
  const sheet = within(screen.getByRole("dialog", { name: "Product render" }));
  await user.click(sheet.getByText("Input details"));
  expect(sheet.queryByText(/full manifest note/)).toBeNull();
});

it("puts manifest details behind a closed native disclosure", async () => {
  const user = userEvent.setup();
  renderWorkflows(ownerViewer);
  await user.click(await screen.findByRole("button", { name: "Details for Product render" }));
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
  renderWorkflows(ownerViewer);
  await user.click(await screen.findByRole("button", { name: "Details for Product render" }));
  const sheet = within(screen.getByRole("dialog", { name: "Product render" }));
  await user.click(sheet.getByText("Technical details"));
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

it("shows the owner one specific reason and a next step for a workflow that is not set up", async () => {
  const user = userEvent.setup();
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) =>
    Response.json(String(input).includes("/me/api/media") ? { assets: [] } : {
      ...catalog,
      workflows: catalog.workflows.map((workflow) => ({
        ...workflow,
        availability: { status: "unavailable", available: false, reason_code: "unavailable_disabled" },
      })),
    }),
  ));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><ViewerContext.Provider value={ownerViewer}><WorkflowsPage /></ViewerContext.Provider></QueryClientProvider>);
  await screen.findByText("Not set up (1)");
  await user.click(screen.getByText("Not set up (1)"));
  const card = within(screen.getByRole("listitem", { name: "Product render" }));
  expect(card.getByText("Turned off. Turn it on in Engines.")).toBeVisible();
  expect(card.getByRole("link", { name: "Open Engines" })).toHaveAttribute("href", "/engines?service=image-local");
  expect(card.queryByRole("link", { name: "Open in Try" })).toBeNull();
  expect(card.queryByRole("button", { name: "Open in Try" })).toBeNull();
  await user.click(card.getByRole("button", { name: "Details for Product render" }));
  const sheet = within(screen.getByRole("dialog", { name: "Product render" }));
  expect(sheet.getByText("Turned off. Turn it on in Engines.")).toBeVisible();
  expect(sheet.getByRole("link", { name: "Open Engines" })).toBeVisible();
  expect(sheet.queryByText(/Not available right now/)).toBeNull();
  expect(sheet.getByText("Technical details").closest("details")).not.toHaveAttribute("open");
});

it("keeps available workflow Run links unchanged on cards and in details", async () => {
  const user = userEvent.setup();
  renderWorkflows();
  const card = within(await screen.findByRole("listitem", { name: "Product render" }));
  expect(card.getByRole("link", { name: "Open in Try" })).toHaveAttribute("href", "/try?workflow=image.product-render");
  await user.click(card.getByRole("button", { name: "Details for Product render" }));
  const sheet = within(screen.getByRole("dialog", { name: "Product render" }));
  expect(sheet.getByRole("link", { name: "Open in Try" })).toHaveAttribute("href", "/try?workflow=image.product-render");
});

it("updates an open details sheet when a refresh makes its workflow unavailable", async () => {
  const user = userEvent.setup();
  let available = true;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) =>
    Response.json(String(input).includes("/me/api/media") ? { assets: [] } : {
      ...catalog,
      workflows: catalog.workflows.map((workflow) => ({
        ...workflow,
        availability: available
          ? workflow.availability
          : { status: "unavailable", available: false, reason_code: "unavailable_disabled" },
      })),
    }),
  ));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><WorkflowsPage /></QueryClientProvider>);
  const card = within(await screen.findByRole("listitem", { name: "Product render" }));
  await user.click(card.getByRole("button", { name: "Details for Product render" }));
  const sheet = within(screen.getByRole("dialog", { name: "Product render" }));
  expect(sheet.getByRole("link", { name: "Open in Try" })).toBeInTheDocument();
  available = false;
  await act(async () => {
    await client.refetchQueries({ queryKey: ["workflows", "catalog"] });
  });
  const sheetRun = await within(screen.getByRole("dialog", { name: "Product render" }))
    .findByRole("button", { name: "Open in Try" });
  expect(sheetRun).toBeDisabled();
  expect(sheetRun.nextElementSibling).toHaveTextContent(
    "Not available right now. The person who manages Azizam can set it up.",
  );
  expect(sheetRun.nextElementSibling).not.toHaveTextContent(/Unavailable/);
  client.clear();
});

it("opens workflow details from the list row and exposes named capability segments", async () => {
  const user = userEvent.setup();
  renderWorkflows();
  const list = await screen.findByRole("list", { name: "Pictures workflows" });
  const row = within(list).getByRole("listitem", { name: "Product render" });
  expect(row).toHaveAttribute("data-lines", "2");
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Workflows1");
  const filters = within(screen.getByRole("group", { name: "Workflow capability" }));
  expect(filters.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "true");
  await user.click(filters.getByRole("button", { name: "Images" }));
  expect(filters.getByRole("button", { name: "Images" })).toHaveAttribute("aria-pressed", "true");
  await user.click(within(row).getByText("From text → a picture"));
  expect(screen.getByRole("dialog", { name: "Product render" })).toBeVisible();
});
