import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import WorkflowsPage from "./page";

const workflows = [
  {
    id: "workflow.image.upscale",
    capability: "image",
    title: "Upscale image",
    purpose: "Upscale and restore an image",
    description: "Make an image larger without changing the prompt.",
    inputs: [
      {
        name: "image",
        type: "string",
        required: true,
        accepted: true,
        description: "PNG or JPEG image input.",
      },
      {
        name: "strength",
        type: "number",
        minimum: 0,
        maximum: 1,
        accepted: true,
      },
    ],
    outputs: [{ name: "image", media_type: "image/png" }],
    engine_binding: null,
    availability: { status: "ready", reason_code: "ready" },
    typical_seconds: null,
    aliases: ["image/upscale"],
    examples: { curl: "curl /workflow", python: "run()", javascript: "run()" },
    native_route: null,
    discovery_url: "/me/api/workflows/workflow.image.upscale",
  },
  {
    id: "workflow.text.clean",
    capability: "workflow",
    title: "Clean text",
    description: "Normalize text before using it in another request.",
    inputs: [
      {
        name: "text",
        type: "string",
        required: true,
        accepted: true,
      },
    ],
    outputs: [{ name: "text", media_type: "text/plain" }],
    engine_binding: null,
    availability: { status: "ready", reason_code: "ready" },
    typical_seconds: 2,
    aliases: [],
    examples: { curl: "curl /workflow", python: "run()", javascript: "run()" },
    native_route: null,
    discovery_url: "/me/api/workflows/workflow.text.clean",
  },
];

function renderWorkflows() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <WorkflowsPage />
    </QueryClientProvider>,
  );
}

describe("WorkflowsPage empathy copy", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("/me/api/media")) return Response.json({ assets: [] });
        return Response.json({ schema_version: 1, workflows });
      }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  test("cards show input icons, hide technical rows and filter by capability", async () => {
    renderWorkflows();

    expect(await screen.findByText("Upscale image")).toBeVisible();
    expect(screen.getByText("Upscale and restore an image")).toBeVisible();
    expect(screen.getByText("Clean text")).toBeVisible();
    expect(screen.getByLabelText("Image input")).toBeVisible();
    expect(screen.getByText("Needs an image")).toBeVisible();
    expect(screen.getByLabelText("Text input")).toBeVisible();
    expect(screen.queryByText("image/png")).not.toBeInTheDocument();
    expect(screen.queryByText("2.0 s")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Has image" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Images" }));

    expect(screen.getByText("Upscale image")).toBeVisible();
    expect(screen.queryByText("Clean text")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Image input")).toBeVisible();
  });

  test("detail sheet uses plain input copy and preserves exact workflow values", async () => {
    renderWorkflows();

    await screen.findByText("Upscale image");
    fireEvent.click(screen.getAllByRole("button", { name: "Details" })[0]);

    await waitFor(() => expect(screen.getByText("Purpose")).toBeVisible());
    expect(
      screen.getByText("Inputs, example requests and recent outputs."),
    ).toBeVisible();
    expect(screen.getByText("Required inputs")).toBeVisible();
    expect(screen.getAllByText("image").length).toBeGreaterThan(0);
    expect(screen.getByText("workflow.image.upscale")).toBeInTheDocument();
    expect(screen.queryByText("Typed inputs")).not.toBeInTheDocument();
  });
});
