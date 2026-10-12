import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { ViewerContext, type Viewer } from "@/components/shell/session";
import {
  composedRows,
  faceLikenessRow,
  seededCatalog,
  seededRecentJobs,
} from "../../../e2e/fixtures/workflows-seeded";
import { workflowsPolishCatalog } from "../../../e2e/fixtures/workflows-polish";
import WorkflowsPage from "./page";

const member: Viewer = {
  authenticated: true,
  name: "Sam",
  email: null,
  role: "member",
  admin: false,
};
const owner: Viewer = { ...member, name: "Tim", role: "owner", admin: true };

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

function renderWorkflows(viewer: Viewer = member) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ViewerContext.Provider value={viewer}>
        <WorkflowsPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}

/** The catalog row for Face Likeness Check as /me/api/workflows returns it. */
const faceLikeness = {
  ...workflows[0],
  id: "image.analyze.face-likeness",
  title: "Face Likeness Check",
  purpose: "Score how closely a face matches a reference",
  inputs: [
    { name: "reference", type: "string", asset: true, media_type: "image/*", label: "a face photo", required: true, accepted: true },
    { name: "candidate", type: "string", asset: true, media_type: "image/*", label: "a photo to check", required: true, accepted: true },
    { name: "people", type: "string", label: "a list of people", required: false, accepted: true },
    { name: "thresholds", type: "string", label: "custom thresholds", required: false, accepted: true },
  ],
  outputs: [{ name: "report", media_type: "application/json", label: "a likeness score" }],
  engine_binding: "gateway",
  availability: { status: "unknown", reason_code: "unknown", available: false, measured: false, evidence: { kind: "unknown" } },
};

function serve(rows: unknown[]) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) =>
    url.includes("/me/api/media")
      ? Response.json({ assets: [] })
      : Response.json({ schema_version: 1, workflows: rows }),
  ));
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

  test("rows expose required inputs and honest missing-input summaries before opening details", async () => {
    serve([
      { ...workflows[0], inputs: [{ name: "optional", type: "number", accepted: true, required: false }] },
      { ...workflows[1], inputs: [] },
    ]);
    renderWorkflows();
    const picture = await screen.findByRole("listitem", { name: "Upscale image" });
    expect(within(picture).getByText("From no required inputs → a picture")).toBeVisible();
    expect(within(picture).getByText("From no required inputs → a picture").closest("p"))
      .toHaveAttribute("title", "From no required inputs → a picture");
    expect(screen.getByText("From inputs not listed → text")).toBeVisible();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("keeps the internal workflow ID behind Technical details and a copy action", async () => {
    renderWorkflows(owner);
    const card = await screen.findByRole("listitem", { name: "Upscale image" });
    expect(within(card).queryByText("workflow.image.upscale", { exact: true })).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: "Details for Upscale image" }));
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.getByText("workflow.image.upscale", { exact: true })).not.toBeVisible();
    fireEvent.click(sheet.getByText("Technical details", { exact: true }));
    expect(sheet.getByText("workflow.image.upscale", { exact: true })).toBeVisible();
    expect(sheet.getByRole("button", { name: "Copy workflow ID" })).toBeVisible();
  });

  test("whole rows open details while the compact quiet Try action remains independent", async () => {
    renderWorkflows();
    const row = await screen.findByRole("listitem", { name: "Upscale image" });
    const actions = within(row).getAllByRole("link", { name: "Open in Try" });
    expect(actions).toHaveLength(1);
    expect(actions[0]).not.toHaveClass("bg-primary");
    expect(actions[0]).toHaveAttribute("href", "/try?workflow=workflow.image.upscale");
    actions[0].addEventListener("click", (event) => event.preventDefault());
    fireEvent.click(actions[0]);
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(row);
    expect(screen.getByRole("dialog")).toBeVisible();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const trigger = within(row).getByRole("button", { name: "Details for Upscale image" });
    trigger.focus();
    expect(trigger).toHaveFocus();
    fireEvent.click(trigger);
    expect(screen.getByRole("dialog")).toBeVisible();
  });

  test("groups once by declared outputs with counts that follow search and capability filters", async () => {
    const catalog = [
      { ...workflows[0], id: "picture.one", title: "Picture one", capability: "video" },
      { ...workflows[0], id: "picture.two", title: "Picture two", capability: "audio" },
      { ...workflows[0], id: "video.one", title: "Video one", outputs: [{ name: "clip", media_type: "video/mp4" }] },
      { ...workflows[0], id: "audio.one", title: "Audio one", outputs: [{ name: "clip", media_type: "audio/wav" }] },
      { ...workflows[0], id: "mixed.one", title: "Mixed one", outputs: [{ name: "picture", media_type: "image/png" }, { name: "clip", media_type: "video/mp4" }] },
      { ...workflows[0], id: "other.one", title: "Other one", outputs: [{ name: "report", media_type: "application/json" }] },
      { ...workflows[0], id: "missing.one", title: "Missing one", outputs: [] },
      workflows[1],
    ];
    serve(catalog);
    renderWorkflows();
    await screen.findByText("Picture one");
    for (const name of ["Pictures 2", "Video 1", "Audio 1", "Text 1", "Mixed outputs 1", "Other outputs 1", "Output not listed 1"]) {
      expect(screen.getByRole("heading", { name })).toBeVisible();
    }
    for (const workflow of catalog) {
      expect(screen.getAllByRole("listitem", { name: workflow.title })).toHaveLength(1);
    }
    expect(within(screen.getByRole("list", { name: "Pictures workflows" })).getAllByRole("listitem")).toHaveLength(2);
    fireEvent.change(screen.getByRole("textbox", { name: "Search workflows" }), { target: { value: "Picture two" } });
    expect(screen.getByRole("heading", { name: "Pictures 1" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Video 1" })).toBeNull();
    fireEvent.change(screen.getByRole("textbox", { name: "Search workflows" }), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Video" }));
    expect(screen.getByRole("heading", { name: "Pictures 1" })).toBeVisible();
    expect(screen.queryByText("Picture two")).toBeNull();
    expect(screen.queryByRole("heading", { name: "Video 1" })).toBeNull();
  });

  test.each(["owner", "superadmin"] as const)(
    "%s sees the verbatim licence in the detail sheet without an engine binding",
    async (role) => {
      const license = "Example licence: Not For Use in restricted regions";
      serve([{ ...workflows[0], license }]);
      renderWorkflows({ ...owner, role });
      fireEvent.click(await screen.findByRole("button", { name: "Details for Upscale image" }));
      const sheet = within(screen.getByRole("dialog"));
      expect(sheet.getByText(`Licence: ${license}`)).toBeVisible();
      expect(sheet.getByText("Licence limit")).toBeVisible();
    },
  );

  test("non-commercial licences receive the warning badge", async () => {
    const license = "Example terms: NON-COMMERCIAL use only";
    serve([{ ...workflows[1], license }]);
    renderWorkflows(owner);
    fireEvent.click(await screen.findByRole("button", { name: "Details for Clean text" }));
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.getByText(`Licence: ${license}`)).toBeVisible();
    expect(sheet.getByText("Licence limit")).toBeVisible();
  });

  test("non-restrictive licences are shown without a warning badge", async () => {
    serve([{ ...workflows[0], license: "Example permissive licence" }]);
    renderWorkflows(owner);
    fireEvent.click(await screen.findByRole("button", { name: "Details for Upscale image" }));
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.getByText("Licence: Example permissive licence")).toBeVisible();
    expect(sheet.queryByText("Licence limit")).toBeNull();
  });

  test.each(["member", "admin"] as const)(
    "%s sees no licence text or badge in the row or detail sheet",
    async (role) => {
      const license = "Example licence: non-commercial use only";
      serve([{ ...workflows[0], license }]);
      renderWorkflows({ ...member, role, admin: role === "admin" });
      await screen.findByRole("button", { name: "Details for Upscale image" });
      expect(screen.queryByText(/Licence:/)).toBeNull();
      expect(screen.queryByText(license, { exact: false })).toBeNull();
      expect(screen.queryByText("Licence limit")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Details for Upscale image" }));
      expect(screen.queryByText(/Licence:/)).toBeNull();
      expect(screen.queryByText(license, { exact: false })).toBeNull();
      expect(screen.queryByText("Licence limit")).toBeNull();
    },
  );

  test("owners see no licence line or badge when metadata is absent", async () => {
    renderWorkflows(owner);
    fireEvent.click(await screen.findByRole("button", { name: "Details for Upscale image" }));
    expect(screen.queryByText(/Licence:/)).toBeNull();
    expect(screen.queryByText("Licence limit")).toBeNull();
  });

  test("cards describe needs and outputs, hide technical rows and filter by capability", async () => {
    renderWorkflows();

    expect(await screen.findByText("Upscale image")).toBeVisible();
    // Rows compare required inputs and outputs; purpose and optional inputs stay in the sheet.
    expect(screen.getByText("Upscale and restore an image")).toBeVisible();
    expect(screen.getByText("Clean text")).toBeVisible();
    expect(screen.queryByText("Needs: a picture")).toBeNull();
    expect(screen.getByText("From a picture → a picture")).toBeVisible();
    expect(screen.queryByText("Needs: text")).toBeNull();
    expect(screen.queryByText("image/png")).not.toBeInTheDocument();
    expect(screen.queryByText("2.0 s")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Has image" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Images" }));

    expect(screen.getByText("Upscale image")).toBeVisible();
    expect(screen.queryByText("Clean text")).not.toBeInTheDocument();
    expect(screen.getByText("From a picture → a picture")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Details for Upscale image" }));
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.getByText("Upscale and restore an image")).toBeVisible();
    expect(sheet.getByText("Needs: a picture")).toBeVisible();
    expect(sheet.getByText("Optional: a number")).toBeVisible();
  });

  test("detail sheet uses plain input copy and preserves exact workflow values", async () => {
    renderWorkflows(owner);

    await screen.findByText("Upscale image");
    fireEvent.click(screen.getAllByRole("button", { name: /^Details for / })[0]);

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
    expect(screen.getByRole("heading", { name: "Pictures 6" })).toBeVisible();
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
    const card = await screen.findByRole("listitem", { name: "Upscale image" });
    expect(within(card).getByText("From a picture and text → a video")).toBeVisible();
    expect(card).not.toHaveTextContent("opaque_");
    expect(card).not.toHaveTextContent("video/mp4");
    expect(card).not.toHaveTextContent("ignored_video");
    fireEvent.click(within(card).getByRole("button", { name: "Details for Upscale image" }));
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.getByText("Needs: a picture and text")).toBeVisible();
    expect(sheet.getByText("Optional: an audio clip")).toBeVisible();
    expect(sheet.getByText("Makes: a video")).toBeVisible();
  });

  test("does not infer a picture from an input name or an output from a capability", async () => {
    const catalog = [{
      ...workflows[0],
      inputs: [{ name: "image", type: "string", accepted: true, required: true }],
      outputs: [],
    }];
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ schema_version: 1, workflows: catalog })));
    renderWorkflows();
    const card = await screen.findByRole("listitem", { name: "Upscale image" });
    expect(within(card).getByText("From text → output not listed")).toBeVisible();
    fireEvent.click(within(card).getByRole("button", { name: "Details for Upscale image" }));
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.getByText("Needs: text")).toBeVisible();
    expect(sheet.getByText("Makes: output not listed")).toBeVisible();
    expect(sheet.queryByText(/Needs: a picture/)).toBeNull();
  });

  test("keeps ignored required inputs out of needs and describes missing input metadata", async () => {
    const catalog = [
      { ...workflows[0], inputs: [{ name: "ignored", type: "image", accepted: false, required: true }] },
      { ...workflows[1], inputs: [] },
    ];
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ schema_version: 1, workflows: catalog })));
    renderWorkflows();
    const first = await screen.findByRole("listitem", { name: "Upscale image" });
    fireEvent.click(within(first).getByRole("button", { name: "Details for Upscale image" }));
    expect(within(screen.getByRole("dialog")).getByText("Needs: no required inputs")).toBeVisible();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const second = screen.getByRole("listitem", { name: workflows[1].title });
    fireEvent.click(within(second).getByRole("button", { name: `Details for ${workflows[1].title}` }));
    expect(within(screen.getByRole("dialog")).getByText("Needs: inputs not listed")).toBeVisible();
  });

  test("puts the next action before folded schema, requests and discovery, with recent outputs last", async () => {
    renderWorkflows(owner);
    await screen.findByText("Upscale image");
    fireEvent.click(screen.getAllByRole("button", { name: /^Details for / })[0]);
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
    const recent = within(dialog).getByRole("region", { name: "Recent creations" });
    expect(details).not.toContainElement(recent);
    expect(details.compareDocumentPosition(recent) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(within(dialog).getByText("Technical details"));
    expect(details).toHaveAttribute("open");
  });

  test("summaries name each input and output in plain words, never 'text and text' or 'a file'", async () => {
    serve([faceLikeness, { ...workflows[0], id: "w.ready", title: "Ready one" }]);
    renderWorkflows(owner);
    fireEvent.click(await screen.findByText("Not set up (1)"));
    const card = screen.getByRole("listitem", { name: "Face Likeness Check" });
    expect(within(card).getByText("From a face photo and a photo to check → a likeness score")).toBeVisible();
    fireEvent.click(within(card).getByRole("button", { name: "Details for Face Likeness Check" }));
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.getByText("Needs: a face photo and a photo to check")).toBeVisible();
    expect(sheet.getByText("Optional: a list of people and custom thresholds")).toBeVisible();
    expect(sheet.getByText("Makes: a likeness score")).toBeVisible();
    expect(card).not.toHaveTextContent(/text and text|a file/);
    expect(screen.getByRole("dialog")).not.toHaveTextContent(/text and text|a file/);
  });

  test("without declared labels it falls back to the media kind, then to the input name, and de-duplicates", async () => {
    serve([{
      ...workflows[0],
      inputs: [
        { name: "reference", type: "string", asset: true, media_type: "image/*", required: true, accepted: true },
        { name: "candidate", type: "string", asset: true, media_type: "image/*", required: true, accepted: true },
        { name: "clip", type: "string", asset: true, required: true, accepted: true },
        { name: "people", type: "string", required: false, accepted: true },
        { name: "thresholds", type: "string", required: false, accepted: true },
        { name: "width", type: "integer", required: false, accepted: true },
      ],
      outputs: [{ name: "report", media_type: "application/json" }],
    }]);
    renderWorkflows();
    const card = await screen.findByRole("listitem", { name: "Upscale image" });
    expect(within(card).getByText("From a picture for reference, a picture for candidate and a file → a report")).toBeVisible();
    fireEvent.click(within(card).getByRole("button", { name: "Details for Upscale image" }));
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.getByText("Needs: a picture for reference, a picture for candidate and a file")).toBeVisible();
    expect(sheet.getByText("Optional: text for people, text for thresholds and a number")).toBeVisible();
    expect(sheet.getByText("Makes: a report")).toBeVisible();
  });

  test("members do not see workflows that are not set up, but still see ones that are only busy", async () => {
    serve([
      { ...workflows[0], id: "w.ready", title: "Ready one" },
      faceLikeness,
      { ...workflows[0], id: "w.off", title: "Turned off", availability: { status: "unavailable", reason_code: "unavailable_disabled", available: false } },
      { ...workflows[0], id: "w.busy", title: "Busy one", availability: { status: "busy", reason_code: "busy", available: false } },
    ]);
    renderWorkflows(member);
    expect(await screen.findByText("Ready one")).toBeVisible();
    expect(screen.getByText("Busy one")).toBeVisible();
    expect(screen.queryByText("Face Likeness Check")).toBeNull();
    expect(screen.queryByText("Turned off")).toBeNull();
    expect(screen.queryByText(/Not set up/)).toBeNull();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Workflows2");
    expect(document.querySelector('a[href^="/engines"], a[href^="/status"], a[href^="/people"]')).toBeNull();
  });

  test("a member whose every workflow is not set up sees the empty state", async () => {
    serve([faceLikeness]);
    renderWorkflows(member);
    expect(await screen.findByText("No workflows are available to you.")).toBeVisible();
    expect(screen.getByText("Ask the owner what you can use.")).toBeVisible();
  });

  test("a delegated admin gets the member view, not the owner's details", async () => {
    serve([{ ...workflows[0], id: "w.ready", title: "Ready one" }, faceLikeness]);
    renderWorkflows({ ...member, name: "Robin", role: "admin", admin: true });
    expect(await screen.findByText("Ready one")).toBeVisible();
    expect(screen.queryByText(/Not set up/)).toBeNull();
  });

  test("the owner finds unavailable workflows in a folded Not set up section", async () => {
    serve([{ ...workflows[0], id: "w.ready", title: "Ready one" }, faceLikeness]);
    renderWorkflows(owner);
    await screen.findByText("Ready one");
    const section = screen.getByText("Not set up (1)").closest("details")!;
    expect(section).not.toHaveAttribute("open");
    expect(within(section).getByRole("listitem", { name: "Face Likeness Check", hidden: true })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Workflows2");
    fireEvent.click(screen.getByText("Not set up (1)"));
    expect(section).toHaveAttribute("open");
  });

  test.each([
    [{ status: "unavailable", reason_code: "unavailable_disabled" }, "Turned off. Turn it on in Engines.", "Open Engines", "/engines"],
    [{ status: "unavailable", reason_code: "unavailable_unconfigured" }, "Setup is not finished. Finish it in Engines.", "Open Engines", "/engines"],
    [{ status: "not_available", reason_code: "not_available" }, "No engine is set up for it. Add one in Engines.", "Open Engines", "/engines"],
    [{ status: "unavailable", reason_code: "unavailable" }, "Its engine is not answering. See Status.", "Open Status", "/status"],
    [{ status: "unknown", reason_code: "unknown" }, "No health check has reported on it yet. See Status.", "Open Status", "/status"],
    [{ status: "unavailable", reason_code: "policy_denied" }, "An access plan blocks it. Review plans in People.", "Open access plans", "/people?tab=plans"],
  ])("the owner gets one specific reason and a real next step for %j", async (availability, line, linkName, href) => {
    serve([{ ...workflows[0], availability: { ...availability, available: false } }]);
    renderWorkflows(owner);
    const card = await screen.findByRole("listitem", { name: "Upscale image", hidden: true });
    expect(within(card).getByText(line)).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: linkName, hidden: true })).toHaveAttribute("href", href);
    expect(card).not.toHaveTextContent(/Unavailable\.|Not available right now/);
    expect(within(card).queryByRole("link", { name: "Open in Try", hidden: true })).toBeNull();
  });

  test("the engine link goes to the one engine the workflow is bound to", async () => {
    serve([{ ...workflows[0], engine_binding: "comfyui", availability: { status: "unavailable", reason_code: "unavailable_disabled", available: false } }]);
    renderWorkflows(owner);
    const card = await screen.findByRole("listitem", { name: "Upscale image", hidden: true });
    expect(within(card).getByRole("link", { name: "Open Engines", hidden: true })).toHaveAttribute("href", "/engines?service=comfyui");
  });

  test("busy and starting workflows keep a disabled Try button and one retry line", async () => {
    serve([{ ...workflows[0], availability: { status: "starting", reason_code: "starting", available: false } }]);
    renderWorkflows(member);
    const card = await screen.findByRole("listitem", { name: "Upscale image" });
    expect(within(card).getByRole("button", { name: "Open in Try" })).toBeDisabled();
    expect(within(card).getByText("Try again in a moment.")).toBeVisible();
    expect(within(card).getAllByText(/Starting/)).toHaveLength(1);
  });

  test("an empty catalog says who can add workflows without directing members to owner pages", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ schema_version: 1, workflows: [] })));
    renderWorkflows();
    expect(await screen.findByText("No workflows are available to you.")).toBeVisible();
    expect(screen.getByText("Ask the owner what you can use.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Explore models" })).toHaveAttribute("href", "/models");
    expect(document.querySelector('a[href="/engines"], a[href="/status"], a[href="/people"]')).toBeNull();
  });

  test.each([member, { ...member, role: "admin", admin: true }, owner])(
    "COPY-01 workflow details for $role",
    async (viewer) => {
      serve([{
        ...workflows[0],
        engine_binding: "private-engine",
        native_route: "/private-native-route",
        aliases: [],
      }]);
      renderWorkflows(viewer);
      fireEvent.click(await screen.findByRole("button", { name: "Details for Upscale image" }));
      const sheet = within(screen.getByRole("dialog"));
      const isOwner = viewer.role === "owner";
      fireEvent.click(sheet.getByText(isOwner ? "Technical details" : "Input details"));
      expect(sheet.getByText("Task")).toBeVisible();
      expect(sheet.getByRole("heading", { name: "Model names" })).toBeVisible();
      expect(sheet.getByText("No other model names")).toBeVisible();
      for (const value of ["Engine", "private-engine", "Native route", "/private-native-route"]) {
        if (isOwner) expect(sheet.getByText(value)).toBeVisible();
        else expect(sheet.queryByText(value)).toBeNull();
      }
      expect(sheet.queryByText("Engine binding")).toBeNull();
      expect(sheet.queryByText("Stable aliases")).toBeNull();
    },
  );

  test.each([member, owner])("COPY-01 missing engine for $role", async (viewer) => {
    renderWorkflows(viewer);
    fireEvent.click(await screen.findByRole("button", { name: "Details for Upscale image" }));
    const sheet = within(screen.getByRole("dialog"));
    fireEvent.click(sheet.getByText(viewer.admin ? "Technical details" : "Input details"));
    for (const value of ["No engine configured", "No native passthrough route configured"]) {
      if (viewer.admin) expect(sheet.getByText(value)).toBeVisible();
      else expect(sheet.queryByText(value)).toBeNull();
    }
  });

  test("COPY-09 owner empty catalog", async () => {
    serve([]);
    renderWorkflows(owner);
    expect(await screen.findByText("No workflows are available.")).toBeVisible();
    expect(screen.queryByText(/Ask the owner/)).toBeNull();
  });

  test("the page subtitle tells people how to choose a workflow", async () => {
    renderWorkflows();
    await screen.findByText("Upscale image");
    expect(screen.getByText("Choose by purpose and inputs.")).toBeVisible();
  });

  test("COPY-14 workflow task filters", async () => {
    renderWorkflows();
    await screen.findByText("Upscale image");
    expect(screen.getByLabelText("Workflow task")).toBeVisible();
    fireEvent.change(screen.getByRole("textbox", { name: "Search workflows" }), {
      target: { value: "does not match" },
    });
    expect(screen.getByText("Clear the search or choose another task.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByText("Upscale image")).toBeVisible();
  });

  // PF-003: a phone picks the task from one labelled select, so no task
  // hides past the edge of a scrolling strip.
  test("the phone task select lists every task and filters the list", async () => {
    renderWorkflows();
    await screen.findByText("Upscale image");
    const task = screen.getByRole("combobox", { name: "Task" });
    // The same choices as the segments on a wide screen, in the same order.
    const segments = within(screen.getByRole("group", { name: "Workflow task" }));
    expect([...(task as HTMLSelectElement).options].map((o) => o.text)).toEqual(
      segments.getAllByRole("button").map((button) => button.textContent),
    );
    expect((task as HTMLSelectElement).options).toHaveLength(3);
    fireEvent.change(task, { target: { value: "image" } });
    expect(screen.getByText("Upscale image")).toBeVisible();
    expect(screen.queryByText("Clean text")).toBeNull();
  });

});
describe("workflow state and payload regressions", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });
  test.each([null, {}, { ...workflows[0], inputs: [null] }, { ...workflows[0], aliases: null }, { ...workflows[0], availability: null }, { ...workflows[0], outputs: [{ name: 4 }] }])(
    "rejects malformed catalog row %# into a retryable error", async (bad) => {
      serve([bad]);
      renderWorkflows();
      expect(await screen.findByText("Workflows catalog could not be loaded. Try again.")).toBeVisible();
      expect(screen.queryByRole("listitem")).toBeNull();
      expect(screen.getByRole("button", { name: /retry|try again/i })).toBeEnabled();
    },
  );

  test.each([{}, { assets: null }, { assets: [null] }, { assets: [{ asset_id: "x", kind: 4 }] }])(
    "rejects malformed recent creations %# without crashing the sheet", async (payload) => {
      vi.stubGlobal("fetch", vi.fn(async (url: string) => Response.json(
        url.includes("/me/api/media") ? payload : { schema_version: 1, workflows: [workflows[0]] },
      )));
      renderWorkflows();
      fireEvent.click(await screen.findByRole("button", { name: "Details for Upscale image" }));
      expect(await screen.findByText("Recent creations could not be loaded. Try again.")).toBeVisible();
    },
  );

  test.each(["reference portrait", "a likeness score", "unhurried detailed purpose"])(
    "searches displayed metadata %s", async (query) => {
      serve([{ ...workflows[0], purpose: "An unhurried detailed purpose that explains the complete result without cutting off the ending",
        inputs: [{ name: "opaque", type: "string", label: "reference portrait", required: true, accepted: true }],
        outputs: [{ name: "opaque", label: "a likeness score" }] }]);
      renderWorkflows();
      await screen.findByText("Upscale image");
      fireEvent.change(screen.getByRole("textbox", { name: "Search workflows" }), { target: { value: query } });
      expect(screen.getByRole("listitem", { name: "Upscale image" })).toBeVisible();
      expect(screen.getByText(/complete result without cutting off the ending/)).toBeVisible();
    },
  );

  test("keeps ids, MIME and JSON defaults out of member input details", async () => {
    serve([{ ...workflows[0], inputs: [{ name: "opaque_input", type: "object", label: "Settings", accepted: true, default: { secret_schema: true }, media_type: "image/png" }] }]);
    renderWorkflows();
    fireEvent.click(await screen.findByRole("button", { name: "Details for Upscale image" }));
    const sheet = within(screen.getByRole("dialog"));
    fireEvent.click(sheet.getByText("Input details"));
    expect(sheet.getByText("Settings: PNG")).toBeVisible();
    for (const text of ["workflow.image.upscale", "image/png", "opaque_input", "secret_schema"]) expect(screen.getByRole("dialog").textContent).not.toContain(text);
    expect(await sheet.findByText("No creations yet. Open this workflow in Try.")).toBeVisible();
  });

  test("does not label a first load as refreshing", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    renderWorkflows();
    expect(screen.getByRole("status", { name: "Loading workflows" })).toBeVisible();
    expect(screen.queryByText("Refreshing")).toBeNull();
  });
});

describe("workflow recovery and empty headings", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });
  test("recovers from a malformed catalog after Retry", async () => {
    let valid = false;
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ schema_version: 1, workflows: valid ? workflows : [null] })));
    renderWorkflows();
    expect(await screen.findByText("Workflows catalog could not be loaded. Try again.")).toBeVisible();
    valid = true;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Upscale image")).toBeVisible();
  });
  test("omits an empty Purpose heading and keeps unavailable creations guidance truthful", async () => {
    serve([{ ...workflows[0], purpose: "", description: "", availability: { status: "busy", reason_code: "busy", available: false } }]);
    renderWorkflows();
    fireEvent.click(await screen.findByRole("button", { name: "Details for Upscale image" }));
    const sheet = within(screen.getByRole("dialog"));
    expect(sheet.queryByRole("heading", { name: "Purpose" })).toBeNull();
    const recent = sheet.getByRole("region", { name: "Recent creations" });
    // The sheet states the busy reason once; the empty list does not repeat it.
    expect(await within(recent).findByText("No creations yet.")).toBeVisible();
    expect(within(recent).queryByText(/Open this workflow in Try|Busy with other requests/)).toBeNull();
    expect(sheet.getAllByText("Busy with other requests. Try again soon.")).toHaveLength(1);
  });
});

/** Serve the catalog and the recent-creations list; remember each media URL asked for. */
function serveWithMedia(
  catalog: unknown,
  media: unknown = { assets: [], models: [], next_cursor: null },
  mediaStatus = 200,
) {
  const mediaUrls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url.includes("/me/api/media")) {
        mediaUrls.push(url);
        return Response.json(media, { status: mediaStatus });
      }
      return Response.json(catalog);
    }),
  );
  return mediaUrls;
}

async function openDetails(name: string, openTechnical = false) {
  fireEvent.click(await screen.findByRole("button", { name: `Details for ${name}` }));
  const sheet = within(screen.getByRole("dialog"));
  if (openTechnical) fireEvent.click(sheet.getByText("Technical details"));
  return sheet;
}

describe("workflow sheet input table and summaries (moved from the browser)", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  test("the wide input table has five columns and shows every default, range and enum", async () => {
    serveWithMedia(workflowsPolishCatalog);
    renderWorkflows(owner);
    const sheet = await openDetails("Product render", true);
    const table = within(sheet.getByLabelText("Scrollable inputs"));
    for (const name of ["Name", "Type", "Default", "Range", "Applies"]) {
      expect(table.getByRole("columnheader", { name })).toBeInTheDocument();
    }
    expect(table.getAllByRole("columnheader")).toHaveLength(5);
    // seed and guidance_scale both default to 0.
    expect(table.getAllByRole("cell", { name: "0" })).toHaveLength(2);
    expect(table.getByRole("cell", { name: "false" })).toBeInTheDocument();
    expect(table.getByRole("cell", { name: "0 to 65535" })).toBeInTheDocument();
    expect(table.getByRole("cell", { name: "0 to 20" })).toBeInTheDocument();
    expect(table.getByText("studio, flatlay")).toBeInTheDocument();
    expect(table.getByText("Ignored")).toBeInTheDocument();
  });

  test("the narrow input summaries show type, requirement, default, range and Applies for each input", async () => {
    serveWithMedia(workflowsPolishCatalog);
    renderWorkflows(owner);
    const sheet = await openDetails("Product render", true);
    const summaries = within(sheet.getByRole("list", { name: "Input summaries" }));
    expect(summaries.getAllByRole("listitem")).toHaveLength(5);
    expect(summaries.getByText("seed")).toBeInTheDocument();
    expect(summaries.getByText("integer · Optional")).toBeInTheDocument();
    expect(summaries.getByText("0 to 65535")).toBeInTheDocument();
    expect(summaries.getByText("guidance_scale")).toBeInTheDocument();
    expect(summaries.getByText("0 to 20")).toBeInTheDocument();
    expect(summaries.getByText("studio, flatlay")).toBeInTheDocument();
    expect(summaries.getByText("Ignored")).toBeInTheDocument();
    expect(summaries.getByText("prompt").closest("li")).toHaveTextContent("string · Required");
  });

  test("an input with no default or range and a workflow with no typical time add no empty facts", async () => {
    serveWithMedia(seededCatalog);
    renderWorkflows(owner);
    fireEvent.click(await screen.findByText("Not set up (1)"));
    const sheet = await openDetails("Voiceover", true);
    expect(sheet.queryByText("Typical time")).toBeNull();
    const summary = within(sheet.getByRole("list", { name: "Input summaries" }));
    expect(summary.queryByText("Default")).toBeNull();
    expect(summary.queryByText("Range")).toBeNull();
    cleanup();
    serveWithMedia(seededCatalog);
    renderWorkflows(owner);
    const product = await openDetails("Product render", true);
    expect(product.getByText("Typical time")).toBeInTheDocument();
    expect(product.getByText("~12 s")).toBeInTheDocument();
  });
});

describe("workflows catalog states (moved from the browser)", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  test("a catalog the gateway refuses (HTTP 503) shows the retryable error and no rows", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      Response.json({ error: { message: "Unavailable for this test" } }, { status: 503 }),
    ));
    renderWorkflows(owner);
    expect(await screen.findByText("Workflows catalog could not be loaded. Try again.")).toBeVisible();
    expect(screen.queryByRole("listitem")).toBeNull();
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  test("the seeded owner catalog groups by output, folds the unset workflow with one reason and hides schema detail", async () => {
    serveWithMedia(seededCatalog);
    renderWorkflows(owner);
    expect(await screen.findByRole("heading", { name: /^Pictures\b/ })).toBeVisible();
    expect(screen.getByRole("button", { name: "Details for Product render" })).toBeVisible();
    fireEvent.click(screen.getByText("Not set up (1)"));
    expect(screen.getByRole("heading", { name: /^Output not listed\b/ })).toBeVisible();
    expect(screen.getByText("Setup is not finished. Finish it in Engines.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Open Engines" })).toBeVisible();
    expect(screen.queryByText(/Long manifest guidance stays searchable/)).toBeNull();
    expect(screen.queryByText("image/png")).toBeNull();
    expect(screen.queryByText("product-shot", { exact: true })).toBeNull();
    expect(screen.queryByText("Unavailable", { exact: true })).toBeNull();
    expect(screen.getAllByRole("button", { name: /^Details for / })).toHaveLength(2);
  });

  test("an empty catalog tells the owner none are available without sending them anywhere", async () => {
    serveWithMedia({ schema_version: 1, workflows: [] });
    renderWorkflows(owner);
    expect(await screen.findByText(/^No workflows are available/)).toBeVisible();
  });
});

describe("workflows search (moved from the browser)", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  test("search finds a workflow by alias, engine, input text, output text, range and route but not by technical notes, and clearing restores the list", async () => {
    serveWithMedia(seededCatalog);
    renderWorkflows(owner);
    const search = await screen.findByRole("textbox", { name: "Search workflows" });
    const product = () => screen.queryByRole("button", { name: "Details for Product render" });
    const voiceover = () => screen.queryByRole("button", { name: "Details for Voiceover", hidden: true });

    fireEvent.change(search, { target: { value: "product-shot" } });
    expect(product()).toBeInTheDocument();
    expect(voiceover()).toBeNull();

    fireEvent.change(search, { target: { value: "does-not-exist" } });
    expect(screen.getByText("No matching workflows")).toBeVisible();
    // Technical notes are shown in the sheet but are not searched.
    fireEvent.change(search, { target: { value: "Long manifest guidance" } });
    expect(screen.getByText("No matching workflows")).toBeVisible();
    expect(product()).toBeNull();

    for (const query of [
      "image-local",
      "What to render",
      "Final product render asset",
      "256 to 2048",
      "/v1/workflows/image.product-render/run",
    ]) {
      fireEvent.change(search, { target: { value: query } });
      expect(product(), query).toBeInTheDocument();
    }
    fireEvent.change(search, { target: { value: "" } });
    expect(product()).toBeInTheDocument();
    fireEvent.click(screen.getByText("Not set up (1)"));
    expect(voiceover()).toBeVisible();
  });

  test("a search that only matches a workflow that is not set up opens the folded section", async () => {
    serveWithMedia(seededCatalog);
    renderWorkflows(owner);
    fireEvent.change(await screen.findByRole("textbox", { name: "Search workflows" }), { target: { value: "speech" } });
    expect(screen.getByRole("button", { name: "Details for Voiceover" })).toBeVisible();
    expect(screen.getByText("Not set up (1)").closest("details")).toHaveAttribute("open");
  });
});

describe("workflow sheet content and recent creations (moved from the browser)", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  test("the owner sheet folds the technical details, then shows the table, ids, code tabs, copy, links and recent jobs", async () => {
    const user = userEvent.setup();
    const mediaUrls = serveWithMedia(seededCatalog, seededRecentJobs);
    renderWorkflows(owner);
    fireEvent.click(await screen.findByRole("button", { name: "Details for Product render" }));
    const dialog = screen.getByRole("dialog");
    const sheet = within(dialog);
    const technical = sheet.getByText("Technical details").closest("details")!;
    expect(technical).not.toHaveAttribute("open");
    fireEvent.click(sheet.getByText("Technical details"));
    expect(technical).toHaveAttribute("open");
    expect(sheet.getByText(/Long manifest guidance stays searchable/)).toBeVisible();
    for (const name of ["Name", "Type", "Default", "Range", "Applies"]) {
      expect(sheet.getByRole("columnheader", { name })).toBeInTheDocument();
    }
    expect(sheet.getByRole("cell", { name: /prompt/ })).toBeInTheDocument();
    expect(sheet.getByRole("cell", { name: /256\s*(?:-|to)\s*2048/ })).toBeInTheDocument();
    expect(sheet.getByRole("cell", { name: "Ignored" })).toBeInTheDocument();

    const facts = within(technical);
    expect(facts.getByText("image.product-render", { exact: true })).toBeVisible();
    expect(facts.getByText("image-local", { exact: true })).toBeVisible();
    expect(facts.getByText("/v1/workflows/image.product-render/run", { exact: true })).toBeVisible();
    expect(facts.getByText("product-shot", { exact: true })).toBeVisible();

    expect(sheet.getByText(/curl \$AZIZAM_BASE/)).toBeVisible();
    await user.click(sheet.getByRole("tab", { name: "Python" }));
    expect(sheet.getByText(/from openai import OpenAI/)).toBeVisible();
    await user.click(sheet.getByRole("tab", { name: "JavaScript" }));
    expect(sheet.getByText(/import OpenAI from "openai"/)).toBeVisible();
    await user.click(sheet.getByRole("button", { name: "Copy JavaScript code" }));
    expect(await navigator.clipboard.readText()).toContain("image.product-render");

    // The seeded rows carry the browser link fields; the page builds its own Try address.
    expect(sheet.getByRole("link", { name: "Open in Try" })).toHaveAttribute("href", "/try?workflow=image.product-render");
    expect(sheet.getByRole("link", { name: "Machine-readable workflow" })).toHaveAttribute("href", "/gateway/v1/workflows/image.product-render");

    const recent = within(await sheet.findByRole("region", { name: "Recent creations" }));
    expect(await recent.findByText("A teal desk lamp on walnut")).toBeVisible();
    expect(recent.getByText("12.4 s")).toBeVisible();
    expect(recent.getAllByText("Image output")).toHaveLength(2);
    expect(dialog.querySelector("time")).not.toBeNull();
    const last = mediaUrls.at(-1)!;
    expect(last).toContain("workflow=image.product-render");
    expect(last).toContain("limit=5");
  });

  test("a workflow with no recent jobs says so and does not invite a Try that cannot run", async () => {
    serveWithMedia(seededCatalog);
    renderWorkflows(owner);
    fireEvent.click(await screen.findByText("Not set up (1)"));
    const sheet = await openDetails("Voiceover");
    expect(await sheet.findByText("No creations yet.")).toBeVisible();
    expect(sheet.queryByText(/Open this workflow in Try/)).toBeNull();
  });

  test("recent creations that the gateway refuses (HTTP 503) show a retryable error in the sheet", async () => {
    serveWithMedia(seededCatalog, { error: { message: "Unavailable" } }, 503);
    renderWorkflows(owner);
    const sheet = await openDetails("Product render");
    expect(await sheet.findByText("Recent creations could not be loaded. Try again.")).toBeVisible();
    expect(sheet.getByRole("button", { name: "Try again" })).toBeEnabled();
  });
});

describe("composed workflow availability names the engine it waits on (moved from the browser)", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  test.each([owner, member])("a $role sees ready, on-demand and down states, folded away from nothing", async (viewer) => {
    serveWithMedia({ schema_version: 1, workflows: composedRows });
    renderWorkflows(viewer);
    await screen.findByRole("listitem", { name: "Identity Scene" });
    // An engine that is down is not a setup task: nothing is folded away.
    expect(screen.queryByText(/Not set up/)).toBeNull();

    const ready = screen.getByRole("listitem", { name: "Identity Scene" });
    expect(within(ready).getByRole("link", { name: "Open in Try" })).toBeVisible();
    expect(ready).not.toHaveTextContent(/Every engine|Unavailable|Ready to start/);

    const startable = screen.getByRole("listitem", { name: "Face Likeness Check" });
    expect(startable).toHaveTextContent("Ready to start");
    expect(startable).toHaveTextContent("Face service starts when you make a request.");
    expect(startable).not.toHaveTextContent(/Unavailable|Not available/);
    expect(within(startable).getByRole("link", { name: "Open in Try" })).toBeVisible();

    const down = screen.getByRole("listitem", { name: "Seamless Tile" });
    expect(down).toHaveTextContent("ComfyUI is down.");
    expect(within(down).getByRole("button", { name: "Open in Try" })).toBeDisabled();
    expect(down).not.toHaveTextContent(/Not available right now|See Status/);
    const engines = within(down).queryByRole("link", { name: "Open Engines" });
    if (viewer.role === "owner") expect(engines).toHaveAttribute("href", "/engines?service=comfyui");
    else expect(engines).toBeNull();
  });

  test("a Face Likeness Check that no health check has reported on reads in plain words with its reason and Status link", async () => {
    serveWithMedia({ schema_version: 1, workflows: [seededCatalog.workflows[0], faceLikenessRow] });
    renderWorkflows(owner);
    fireEvent.click(await screen.findByText("Not set up (1)"));
    const card = screen.getByRole("listitem", { name: "Face Likeness Check" });
    expect(card).toHaveTextContent("From a face photo and a photo to check → a likeness score");
    expect(card).toHaveTextContent("No health check has reported on it yet. See Status.");
    expect(card).not.toHaveTextContent(/Unavailable|Not available right now|text and text/);
    expect(within(card).getByRole("link", { name: "Open Status" })).toHaveAttribute("href", expect.stringMatching(/\/status/));
  });
});
