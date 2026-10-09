import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
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
        media_type: "image/png",
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

  test("cards describe needs and outputs, hide technical rows and filter by capability", async () => {
    renderWorkflows();

    expect(await screen.findByText("Upscale image")).toBeVisible();
    expect(screen.getByText("Upscale and restore an image")).toBeVisible();
    expect(screen.getByText("Clean text")).toBeVisible();
    expect(screen.getByText("Needs: a picture")).toBeVisible();
    expect(screen.getByText("Optional: a number")).toBeVisible();
    expect(screen.getByText("Makes: a picture")).toBeVisible();
    expect(screen.getByText("Needs: text")).toBeVisible();
    expect(screen.queryByText("image/png")).not.toBeInTheDocument();
    expect(screen.queryByText("2.0 s")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Has image" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Images" }));

    expect(screen.getByText("Upscale image")).toBeVisible();
    expect(screen.queryByText("Clean text")).not.toBeInTheDocument();
    expect(screen.getByText("Needs: a picture")).toBeVisible();
  });

  test("detail sheet uses plain input copy and preserves exact workflow values", async () => {
    renderWorkflows();

    await screen.findByText("Upscale image");
    fireEvent.click(screen.getAllByRole("button", { name: "Details" })[0]);

    await waitFor(() => expect(screen.getByText("Purpose")).toBeVisible());
    expect(
      screen.getByText("What you need, what you get and how to start."),
    ).toBeVisible();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Needs: a picture")).toBeVisible();
    expect(screen.getAllByText("image").length).toBeGreaterThan(0);
    expect(screen.getByText("workflow.image.upscale")).toBeInTheDocument();
    expect(screen.queryByText("Typed inputs")).not.toBeInTheDocument();
  });

  test("WORKFLOWS-01 renders each filtered workflow once across all capabilities", async () => {
    const catalog = [
      "image",
      "chat",
      "transcribe",
      "decide",
      "embed",
      "custom_task",
    ].map((capability, index) => ({
      ...workflows[0],
      id: `workflow.test.${index}`,
      capability,
      title: `Catalog item ${index}`,
    }));
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({ schema_version: 1, workflows: catalog }),
      ),
    );
    renderWorkflows();

    await screen.findByText("Catalog item 0");
    for (const row of catalog) {
      expect(screen.getAllByText(row.title)).toHaveLength(1);
    }
    expect(screen.getByRole("heading", { name: "Custom task" })).toBeVisible();
    fireEvent.change(
      screen.getByRole("textbox", { name: "Search workflows" }),
      {
        target: { value: "Catalog item 5" },
      },
    );
    expect(screen.getAllByText("Catalog item 5")).toHaveLength(1);
    expect(screen.queryByText("Catalog item 0")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Custom task" }));
    expect(screen.getAllByText("Catalog item 5")).toHaveLength(1);
  });

  test("uses accepted metadata for required, optional and output summaries, never names or capability", async () => {
    const catalog = [{
      ...workflows[0],
      capability: "image",
      inputs: [
        { name: "opaque_1", type: "string", accepted: true, required: true, accepted_media_types: ["image/png", "image/jpeg"] },
        { name: "opaque_2", type: "string", accepted: true, required: true },
        { name: "opaque_3", type: "file", accepted: true, required: false, media_type: "audio/wav" },
        { name: "ignored_video", type: "file", accepted: false, required: true, media_type: "video/mp4" },
      ],
      outputs: [{ name: "not_a_video_name", media_type: "video/mp4" }],
    }];
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ schema_version: 1, workflows: catalog })));
    renderWorkflows();
    const card = await screen.findByRole("article", { name: "Upscale image" });
    expect(within(card).getByText("Needs: a picture and text")).toBeVisible();
    expect(within(card).getByText("Optional: an audio clip")).toBeVisible();
    expect(within(card).getByText("Makes: a video")).toBeVisible();
    expect(card).not.toHaveTextContent("opaque_");
    expect(card).not.toHaveTextContent("video/mp4");
    expect(card).not.toHaveTextContent("ignored_video");
  });

  test("does not infer a picture from an input name or an output from a capability", async () => {
    const catalog = [{
      ...workflows[0],
      inputs: [{ name: "image", type: "string", accepted: true, required: true }],
      outputs: [],
    }];
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ schema_version: 1, workflows: catalog })));
    renderWorkflows();
    const card = await screen.findByRole("article", { name: "Upscale image" });
    expect(within(card).getByText("Needs: text")).toBeVisible();
    expect(within(card).getByText("Makes: output not listed")).toBeVisible();
    expect(within(card).queryByText(/Needs: a picture/)).toBeNull();
  });

  test("keeps ignored required inputs out of needs and describes missing input metadata", async () => {
    const catalog = [
      { ...workflows[0], inputs: [{ name: "ignored", type: "image", accepted: false, required: true }] },
      { ...workflows[1], inputs: [] },
    ];
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ schema_version: 1, workflows: catalog })));
    renderWorkflows();
    expect(await screen.findByText("Needs: no required inputs")).toBeVisible();
    expect(screen.getByText("Needs: inputs not listed")).toBeVisible();
  });

  test("puts the next action before folded schema, requests and discovery, with recent outputs last", async () => {
    renderWorkflows();
    await screen.findByText("Upscale image");
    fireEvent.click(screen.getAllByRole("button", { name: "Details" })[0]);
    const dialog = screen.getByRole("dialog");
    const details = within(dialog).getByText("Technical details").closest("details")!;
    const action = within(dialog).getByRole("link", { name: "Open in Try" });
    const needs = within(dialog).getByText("Needs: a picture");
    expect(action).toHaveAttribute("href", "/try?workflow=workflow.image.upscale");
    expect(needs.compareDocumentPosition(action) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(action.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(details).not.toHaveAttribute("open");
    expect(details).toContainElement(within(dialog).getByText("Inputs", { selector: "h3" }));
    expect(details).toContainElement(within(dialog).getByText("Example request"));
    expect(details).toContainElement(within(dialog).getByText("Discovery list"));
    expect(details).toHaveTextContent("image: image/png");
    expect(details).toContainElement(within(dialog).getByText("workflow.image.upscale"));
    const recent = within(dialog).getByRole("region", { name: "Recent jobs" });
    expect(details).not.toContainElement(recent);
    expect(details.compareDocumentPosition(recent) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(within(dialog).getByText("Technical details"));
    expect(details).toHaveAttribute("open");
  });

  test("unavailable cards and details give the setup next step without an owner-only link", async () => {
    const catalog = [{
      ...workflows[0],
      availability: { status: "not_configured", reason_code: "not_configured", available: false },
    }];
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      url.includes("/me/api/media")
        ? Response.json({ assets: [] })
        : Response.json({ schema_version: 1, workflows: catalog }),
    ));
    renderWorkflows();
    const card = await screen.findByRole("article", { name: "Upscale image" });
    const nextStep = "Not available right now. The person who manages Azizam can set it up.";
    expect(within(card).getByText(nextStep)).toBeVisible();
    expect(within(card).queryByRole("link", { name: "Open in Try" })).toBeNull();
    expect(within(card).getByRole("button", { name: "Open in Try" })).toBeDisabled();
    fireEvent.click(within(card).getByRole("button", { name: "Details" }));
    const dialog = screen.getByRole("dialog");
    const message = within(dialog).getByText(nextStep);
    const details = within(dialog).getByText("Technical details").closest("details")!;
    expect(message).toBeVisible();
    expect(message.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(dialog).queryByRole("link", { name: "Open in Try" })).toBeNull();
    expect(dialog.querySelector('a[href="/engines"], a[href="/status"], a[href="/people"]')).toBeNull();
  });

  test("an empty catalog says who can add workflows without directing members to owner pages", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ schema_version: 1, workflows: [] })));
    renderWorkflows();
    expect(await screen.findByText("No workflows are set up yet. The person who manages Azizam can add them.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Explore models" })).toHaveAttribute("href", "/models");
    expect(document.querySelector('a[href="/engines"], a[href="/status"], a[href="/people"]')).toBeNull();
  });

});
