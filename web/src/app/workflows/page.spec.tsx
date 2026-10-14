import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
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
import { isWorkflowCatalog, readJson, readRecentAssets } from "./workflow-api";

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

  test("names the page with exactly one level-one heading", async () => {
    renderWorkflows();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1, name: /^Workflows/ })).toBeVisible();
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


describe("Workflows remaining interactive gaps", () => {
  afterEach(() => {
    cleanup();
    for (const client of gapClients.splice(0)) client.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  // Mutation proof: removing owner from the rows memo dependencies leaves the privileged row visible.
  test("H01 changing owner to member recalculates the list, count and task options without refetching", async () => {
    const user = userEvent.setup();
    const fetcher = gapServe(() => Response.json(gapCatalog([
      gapRow(),
      gapRow({ id: "hidden.voice", title: "Unconfigured narration", capability: "voice",
        availability: { status: "unavailable", reason_code: "unavailable_unconfigured", available: false } }),
    ])));
    const app = gapMount(owner);
    await screen.findByRole("listitem", { name: workflows[0].title });
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Workflows2");
    expect(Array.from((screen.getByRole("combobox", { name: "Task" }) as HTMLSelectElement).options)
      .map((option) => option.value)).toContain("voice");
    await user.click(screen.getByText("Not set up (1)"));
    expect(screen.getByRole("listitem", { name: "Unconfigured narration" })).toBeVisible();

    app.setViewer(member);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Workflows1");
    expect(screen.queryByRole("listitem", { name: "Unconfigured narration" })).toBeNull();
    expect(screen.queryByText("Not set up (1)")).toBeNull();
    expect(Array.from((screen.getByRole("combobox", { name: "Task" }) as HTMLSelectElement).options)
      .map((option) => option.value)).not.toContain("voice");
    expect(screen.getByRole("listitem", { name: workflows[0].title })).toBeVisible();
    expect(gapCatalogCalls(fetcher)).toHaveLength(1);
  });

  // Mutation proof: String(value) loses structured defaults as [object Object].
  test("H02 owner input defaults preserve structured JSON in both responsive views", async () => {
    const user = userEvent.setup();
    const value = { mode: "precise", layers: [0, false] };
    gapServe(() => Response.json(gapCatalog([gapRow({
      inputs: [{ name: "settings", type: "object", accepted: true, default: value }],
    })])));
    gapMount(owner);
    const sheet = await gapOpen(user);
    await user.click(sheet.getByText("Technical details", { exact: true }));
    const expected = '{"mode":"precise","layers":[0,false]}';
    expect(within(sheet.getByLabelText("Scrollable inputs"))
      .queryByRole("cell", { name: expected })).toBeInTheDocument();
    const item = within(sheet.getByRole("list", { name: "Input summaries" }))
      .getByRole("heading", { name: "settings" }).closest("li")!;
    expect(within(item).getByText("Default", { exact: true }).nextElementSibling?.textContent)
      .toBe(expected);
  });

  // Mutation proof: retrying={false} leaves the retry button enabled during the request.
  test("H03 media Retry disables repeated requests until the pending retry resolves", async () => {
    const user = userEvent.setup();
    const pending = gapDeferred<Response>();
    let attempts = 0;
    const fetcher = gapServe(undefined, () => {
      attempts += 1;
      if (attempts === 1) return Response.json({ assets: [gapAsset({ prompt: "Previous output" })] });
      if (attempts === 2) return new Response(null, { status: 503 });
      return pending.promise;
    });
    const { client } = gapMount();
    const sheet = await gapOpen(user);
    await sheet.findByText("Previous output");
    await act(async () => {
      await client.refetchQueries({ queryKey: ["workflow-media", workflows[0].id], exact: true });
    });
    await sheet.findByText(gapMediaError);
    await user.click(sheet.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(gapMediaCalls(fetcher)).toHaveLength(3));
    const retry = within(sheet.getByRole("region", { name: "Recent creations" })).getByRole("button");
    expect(retry).toBeDisabled();
    await user.click(retry);
    expect(gapMediaCalls(fetcher)).toHaveLength(3);
    await act(async () => {
      pending.resolve(Response.json({ assets: [gapAsset({ prompt: "Retried output" })] }));
    });
    expect(await sheet.findByText("Retried output")).toBeVisible();
    expect(sheet.queryByText(gapMediaError)).toBeNull();
    expect(gapCatalogCalls(fetcher)).toHaveLength(1);
  });

  // Mutation proof: interpolating asset_id without encoding splits the query and adds a fragment.
  test("H04 result navigation preserves reserved characters in the asset identifier", async () => {
    const user = userEvent.setup();
    const id = "image/result & variant=2#crop";
    gapServe(undefined, () => Response.json({ assets: [gapAsset({ asset_id: id })] }));
    gapMount();
    const sheet = await gapOpen(user);
    const recent = within(sheet.getByRole("region", { name: "Recent creations" }));
    const link = await recent.findByRole("link", { name: "Open in Library" });
    const url = gapHref(link);
    expect(["/library", "/library/"]).toContain(url.pathname);
    expect([...url.searchParams.entries()]).toEqual([["asset", id]]);
    expect(url.hash).toBe("");
  });

  // Mutation proof: memoizing availability by workflow ID retains the obsolete runnable action.
  test("H05 an open workflow follows ready-to-busy-to-ready changes including empty-result guidance", async () => {
    const user = userEvent.setup();
    let busy = false;
    gapServe(() => Response.json(gapCatalog([gapRow({
      availability: busy
        ? { status: "busy", reason_code: "busy", available: false }
        : { status: "ready", reason_code: "ready" },
    })])));
    const { client } = gapMount();
    const sheet = await gapOpen(user);
    expect(await sheet.findByText("No creations yet. Open this workflow in Try.")).toBeVisible();
    await waitFor(() => expect(sheet.getByRole("link", { name: "Open in Try" })).toBeVisible());
    busy = true;
    await act(async () => { await client.refetchQueries({ queryKey: ["workflows", "catalog"], exact: true }); });
    await waitFor(() => expect(sheet.queryByRole("button", { name: "Open in Try" })).toBeDisabled(), { onTimeout: (error) => error });
    expect(sheet.queryByRole("link", { name: "Open in Try" })).toBeNull();
    expect(sheet.getByText("No creations yet.", { exact: true })).toBeVisible();
    expect(sheet.queryByText("No creations yet. Open this workflow in Try.")).toBeNull();
    busy = false;
    await act(async () => { await client.refetchQueries({ queryKey: ["workflows", "catalog"], exact: true }); });
    await waitFor(() => expect(sheet.getByRole("link", { name: "Open in Try" })).toBeVisible());
    expect(sheet.queryByRole("button", { name: "Open in Try" })).toBeNull();
    expect(sheet.getByText("No creations yet. Open this workflow in Try.")).toBeVisible();
  });
});

const gapClients: QueryClient[] = [];
const gapCatalogError = "Workflows catalog could not be loaded. Try again.";
const gapMediaError = "Recent creations could not be loaded. Try again.";
const gapRow = (patch: Record<string, unknown> = {}) => ({ ...workflows[0], ...patch });
// gateway/workflows_catalog.py:123-137: retain the real catalog envelope.
// Invalid-shape tests explicitly replace individual values in this envelope.
const gapCatalog = (rows: unknown[] = [gapRow()]) => ({
  schema_version: 1,
  object: "workflow.catalog",
  discovery_url: "/gateway/v1/workflows",
  workflows: rows,
  groups: [],
  counts: { workflows: rows.length, capabilities: 1 },
  links: {
    self: "/me/api/workflows",
    gateway_discovery: "/gateway/v1/workflows",
    openapi: "/openapi.json",
  },
});
const gapAssetId = "img_" + "a".repeat(64);
const gapAsset = (patch: Record<string, unknown> = {}) => ({
  asset_id: gapAssetId,
  kind: "image",
  prompt: null,
  model: null,
  workflow: null,
  created_at: null,
  ...patch,
});
function gapDeferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function gapMount(viewer: Viewer = member) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  gapClients.push(client);
  const tree = (next: Viewer) => (
    <QueryClientProvider client={client}>
      <ViewerContext.Provider value={next}>
        <WorkflowsPage />
      </ViewerContext.Provider>
    </QueryClientProvider>
  );
  const view = render(tree(viewer));
  return { client, ...view, setViewer: (next: Viewer) => view.rerender(tree(next)) };
}
function gapServe(
  catalog: () => Response | Promise<Response> = () => Response.json(gapCatalog()),
  media: () => Response | Promise<Response> = () => Response.json({ assets: [] }),
) {
  // Only page-owned endpoints receive success payloads. Unrelated provider
  // reads stay unavailable instead of being given a fictitious catalog shape.
  const mock = vi.fn(async (url: string, _init?: RequestInit) =>
    url.includes("/me/api/media") ? media()
      : url === "/me/api/workflows" ? catalog() : new Response(null, { status: 404 }),
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}
const gapMediaCalls = (mock: ReturnType<typeof gapServe>) =>
  mock.mock.calls.filter(([url]) => url.includes("/me/api/media"));
const gapCatalogCalls = (mock: ReturnType<typeof gapServe>) =>
  mock.mock.calls.filter(([url]) => url === "/me/api/workflows");
async function gapOpen(user: ReturnType<typeof userEvent.setup>, title = workflows[0].title) {
  await user.click(await screen.findByRole("button", { name: "Details for " + title }));
  return within(screen.getByRole("dialog"));
}
function gapStartRefresh(client: QueryClient, queryKey: string[]) {
  let done!: Promise<void>;
  act(() => { done = client.refetchQueries({ queryKey, exact: true }); });
  return done;
}
function gapHref(link: HTMLElement) {
  return new URL(link.getAttribute("href")!, "https://example.test");
}
function gapFact(scope: HTMLElement, label: string) {
  return within(scope).getByText(label, { exact: true }).parentElement!;
}

describe("Workflows catalog, detail and API branch regressions", () => {
  afterEach(() => {
    cleanup();
    for (const client of gapClients.splice(0)) client.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  // Protects page.tsx:889-890 at f1cac8a.
  // Mutant: Change the initial catalog isPending branch to isError.
  test("G01 initial loading resolves into the actual catalog response", async () => {
    const pending = gapDeferred<Response>();
    const fetcher = gapServe(() => pending.promise);
    gapMount();
    expect(screen.getByRole("status", { name: "Loading workflows" }))
      .toHaveAttribute("aria-busy", "true");
    expect(screen.queryByRole("listitem")).toBeNull();
    expect(screen.queryByText("Refreshing")).toBeNull();
    await act(async () => { pending.resolve(Response.json(gapCatalog())); });
    expect(await screen.findByRole("listitem", { name: workflows[0].title })).toBeVisible();
    expect(screen.queryByRole("status", { name: "Loading workflows" })).toBeNull();
    expect(gapCatalogCalls(fetcher)).toEqual([
      ["/me/api/workflows", { credentials: "same-origin" }],
    ]);
  });

  // Protects page.tsx:893-895 at f1cac8a.
  // Mutant: Replace the catalog onRetry callback with a no-op.
  test.each([
    ["HTTP 503", () => new Response(null, { status: 503 })],
    ["network failure", () => Promise.reject(new TypeError("Network unavailable"))],
    ["invalid JSON", () => new Response("{", { headers: { "content-type": "application/json" } })],
    ["wrong schema version", () => Response.json({ schema_version: 2, workflows: [] })],
    ["missing workflows", () => Response.json({ schema_version: 1 })],
    ["array envelope", () => Response.json([])],
  ] as Array<[string, () => Response | Promise<Response>]>)(
    "G02 catalog %s recovers after one explicit retry",
    async (_name, first) => {
      const user = userEvent.setup();
      let attempt = 0;
      const pending = gapDeferred<Response>();
      const fetcher = gapServe(() => ++attempt === 1 ? first() : pending.promise);
      gapMount();
      expect(await screen.findByText(gapCatalogError)).toBeVisible();
      expect(screen.queryByRole("listitem")).toBeNull();
      await user.click(screen.getByRole("button", { name: "Try again" }));
      await waitFor(() => expect(gapCatalogCalls(fetcher)).toHaveLength(2));
      await act(async () => { pending.resolve(Response.json(gapCatalog())); });
      expect(await screen.findByRole("listitem", { name: workflows[0].title })).toBeVisible();
      expect(screen.queryByText(gapCatalogError)).toBeNull();
      expect(gapCatalogCalls(fetcher).every(([, init]) =>
        init?.credentials === "same-origin",
      )).toBe(true);
    },
  );

  // Protects workflow-api.ts:116-117 at f1cac8a.
  // Mutant: Replace value.workflows.every(workflow) with value.workflows.some(workflow).
  test("G03 a mixed valid/invalid catalog is rejected as a whole", async () => {
    gapServe(() => Response.json(gapCatalog([gapRow(), { ...gapRow(), inputs: [null] }])));
    gapMount();
    expect(await screen.findByText(gapCatalogError)).toBeVisible();
    expect(screen.queryByRole("listitem")).toBeNull();
  });

  // Protects workflow-api.ts:120-125 at f1cac8a.
  // Mutant: Replace value.assets.every(...) with value.assets.some(...).
  test("G03 a mixed valid/invalid assets list is rejected, not partially rendered", async () => {
    const user = userEvent.setup();
    gapServe(undefined, () => Response.json({
      assets: [gapAsset({ prompt: "A valid saved output" }), { asset_id: "broken", kind: 7 }],
    }));
    gapMount();
    const sheet = await gapOpen(user);
    expect(await sheet.findByText(gapMediaError)).toBeVisible();
    expect(sheet.queryByText("A valid saved output")).toBeNull();
    expect(sheet.queryByRole("link", { name: "Open in Library" })).toBeNull();
    expect(sheet.getByRole("link", { name: "Open in Try" })).toBeVisible();
  });

  // Protects page.tsx:843-844 at f1cac8a.
  // Mutant: Memoize detail from allRows using only [detailId] as dependencies.
  test.each(["replace", "remove"] as const)(
    "G04 background refresh keeps current detail until it can %s the selected row",
    async (change) => {
      const user = userEvent.setup();
      const pending = gapDeferred<Response>();
      let attempt = 0;
      gapServe(() => ++attempt === 1 ? Response.json(gapCatalog()) : pending.promise);
      const { client } = gapMount();
      const sheet = await gapOpen(user);
      await sheet.findByText("No creations yet. Open this workflow in Try.");
      const done = gapStartRefresh(client, ["workflows", "catalog"]);
      expect(await screen.findByText("Refreshing")).toBeInTheDocument();
      expect(within(screen.getByRole("dialog")).getByRole("heading", {
        name: workflows[0].title,
      })).toBeVisible();
      const next = change === "replace"
        ? [gapRow({ title: "Updated workflow title", purpose: "Fresh server purpose" })]
        : [];
      await act(async () => {
        pending.resolve(Response.json(gapCatalog(next)));
        await done;
      });
      if (change === "replace") {
        const current = within(screen.getByRole("dialog"));
        expect(await current.findByRole("heading", { name: "Updated workflow title" })).toBeVisible();
        expect(current.getByText("Fresh server purpose")).toBeVisible();
        expect(current.queryByRole("heading", { name: workflows[0].title })).toBeNull();
      } else {
        await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
        expect(await screen.findByText("No workflows are available to you.")).toBeVisible();
      }
      await waitFor(() => expect(screen.queryByText("Refreshing")).toBeNull());
    },
  );

  // Protects page.tsx:891-895 at f1cac8a.
  // Mutant: Guard the catalog error branch with isError && !data.
  test("G05 a failed background catalog refresh hides stale rows and Retry recovers", async () => {
    const user = userEvent.setup();
    let attempt = 0;
    gapServe(() => {
      attempt += 1;
      if (attempt === 2) return Response.json({}, { status: 503 });
      return Response.json(gapCatalog([gapRow({
        title: attempt === 1 ? workflows[0].title : "Recovered workflow",
      })]));
    });
    const { client } = gapMount();
    await screen.findByRole("listitem", { name: workflows[0].title });
    await act(async () => { await client.refetchQueries({ queryKey: ["workflows", "catalog"], exact: true }); });
    expect(await screen.findByText(gapCatalogError)).toBeVisible();
    expect(screen.queryByRole("listitem")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("listitem", { name: "Recovered workflow" })).toBeVisible();
    expect(screen.queryByText(gapCatalogError)).toBeNull();
  });

  // Protects page.tsx:907 at f1cac8a.
  // Mutant: Change the Explore models href from /models to /engines.
  test("G06 owner empty state retains its actual Explore models destination", async () => {
    gapServe(() => Response.json(gapCatalog([])));
    gapMount(owner);
    expect(await screen.findByText("No workflows are available.")).toBeVisible();
    expect(screen.queryByText("Ask the owner what you can use.")).toBeNull();
    expect(screen.getByRole("link", { name: "Explore models" })).toHaveAttribute("href", "/models");
  });

  // Protects page.tsx:826 at f1cac8a.
  // Mutant: Include admin in the owner/superadmin role list.
  test("G07 current role controls privileged details, including superadmin and delegated admin", async () => {
    const user = userEvent.setup();
    gapServe(() => Response.json(gapCatalog([gapRow({
      technical_notes: "Owner research notes",
      license: "Example permissive licence",
      engine_binding: "private-engine",
      native_route: "/private-route",
    })])));
    const app = gapMount({ ...owner, role: "superadmin" });
    const sheet = await gapOpen(user);
    await user.click(sheet.getByText("Technical details", { exact: true }));
    expect(sheet.getByText("Owner research notes")).toBeVisible();
    expect(sheet.getByText("private-engine", { exact: true })).toBeVisible();
    expect(sheet.getByRole("button", { name: "Copy workflow ID" })).toBeVisible();
    expect(sheet.getByRole("link", { name: "Discovery list" })).toBeVisible();
    expect(sheet.getByText("Licence: Example permissive licence")).toBeVisible();

    app.setViewer({ ...member, role: "admin", admin: true });
    const delegated = within(screen.getByRole("dialog"));
    expect(delegated.getByText("Input details", { exact: true })).toBeVisible();
    for (const text of [
      "Owner research notes", "private-engine", "/private-route",
      "Example request", "Licence: Example permissive licence",
    ]) expect(delegated.queryByText(text, { exact: true })).toBeNull();
    expect(delegated.queryByRole("button", { name: "Copy workflow ID" })).toBeNull();
    expect(delegated.queryByRole("link", { name: "Discovery list" })).toBeNull();
    expect(delegated.queryByRole("link", { name: "Machine-readable workflow" })).toBeNull();

    app.setViewer(owner);
    expect(within(screen.getByRole("dialog")).getByText("Owner research notes")).toBeVisible();
  });

  // Protects page.tsx:965-970 at f1cac8a.
  // Mutant: Delete setCapability("all") from Clear filters.
  test("G08 capability/search controls synchronize and Clear filters resets both", async () => {
    const user = userEvent.setup();
    gapServe(() => Response.json(gapCatalog([gapRow(), workflows[1]])));
    gapMount();
    const search = await screen.findByRole("textbox", { name: "Search workflows" });
    const select = screen.getByRole("combobox", { name: "Task" });
    await user.selectOptions(select, "image");
    expect(select).toHaveValue("image");
    expect(screen.queryByRole("listitem", { name: "Clean text" })).toBeNull();
    await user.type(search, "Clean text");
    expect(screen.getByText("No matching workflows")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(search).toHaveValue("");
    expect(select).toHaveValue("all");
    expect(screen.getByRole("listitem", { name: "Clean text" })).toBeVisible();
    await user.click(within(screen.getByRole("group", { name: "Workflow task" }))
      .getByRole("button", { name: "Images" }));
    expect(select).toHaveValue("image");
  });

  // Protects page.tsx:197 at f1cac8a.
  // Mutant: Return false for every nonempty search query.
  test.each([
    "WORKFLOW.IMAGE.UPSCALE", "UPSCALE IMAGE", "unique description token",
    "unique optional noun", "opaque_parameter", "unique input label",
    "output_token", "application/x-branch-report",
  ])("G08 displayed/searchable metadata matches %s", async (query) => {
    const user = userEvent.setup();
    gapServe(() => Response.json(gapCatalog([gapRow({
      purpose: "A simple purpose",
      description: "Unique description token.",
      inputs: [
        { name: "opaque_parameter", type: "string", label: "unique input label", accepted: true, required: true },
        { name: "optional_parameter", type: "string", label: "unique optional noun", accepted: true },
      ],
      outputs: [{ name: "output_token", media_type: "application/x-branch-report" }],
    })])));
    gapMount();
    const search = await screen.findByRole("textbox", { name: "Search workflows" });
    await user.type(search, query);
    expect(screen.getByRole("listitem", { name: workflows[0].title })).toBeVisible();
    await user.clear(search);
    expect(screen.getByRole("listitem", { name: workflows[0].title })).toBeVisible();
  });

  // Protects page.tsx:350 at f1cac8a.
  // Mutant: Remove disabled from the non-runnable Open in Try button.
  test.each([
    ["busy", "Busy with other requests. Try again soon."],
    ["starting", "Try again in a moment."],
  ])("G09 %s disables Try in both row and sheet", async (status, reason) => {
    const user = userEvent.setup();
    gapServe(() => Response.json(gapCatalog([gapRow({
      availability: { status, reason_code: status, available: false },
    })])));
    gapMount();
    const row = await screen.findByRole("listitem", { name: workflows[0].title });
    expect(within(row).getByRole("button", { name: "Open in Try" })).toBeDisabled();
    expect(within(row).queryByRole("link", { name: "Open in Try" })).toBeNull();
    const sheet = await gapOpen(user);
    expect(sheet.getByRole("button", { name: "Open in Try" })).toBeDisabled();
    expect(sheet.getAllByText(reason, { exact: true })).toHaveLength(1);
    expect(await sheet.findByText("No creations yet.")).toBeVisible();
  });

  // Protects page.tsx:349 at f1cac8a.
  // Mutant: Render the disabled Open in Try button even when ownerSetup is true.
  test("G09 owner setup omits a disabled Try and its disclosure can close and reopen", async () => {
    const user = userEvent.setup();
    gapServe(() => Response.json(gapCatalog([gapRow({
      engine_binding: "comfyui",
      availability: { status: "unavailable", reason_code: "unavailable_disabled", available: false },
    })])));
    gapMount(owner);
    const summary = await screen.findByText("Not set up (1)");
    const section = summary.closest("details")!;
    expect(section).not.toHaveAttribute("open");
    await user.click(summary);
    expect(section).toHaveAttribute("open");
    await user.click(summary);
    expect(section).not.toHaveAttribute("open");
    await user.click(summary);
    const row = screen.getByRole("listitem", { name: workflows[0].title });
    expect(within(row).queryByRole("button", { name: "Open in Try" })).toBeNull();
    expect(within(row).queryByRole("link", { name: "Open in Try" })).toBeNull();
    const sheet = await gapOpen(user);
    expect(sheet.queryByRole("button", { name: "Open in Try" })).toBeNull();
    expect(sheet.getByRole("link", { name: "Open Engines" }))
      .toHaveAttribute("href", "/engines?service=comfyui");
  });

  // Protects page.tsx:335 at f1cac8a.
  // Mutant: Interpolate workflow.id directly into the Try URL without encodeURIComponent.
  test("G10 keyboard opens details, ID copies exactly, and navigation preserves real destinations", async () => {
    const user = userEvent.setup();
    const id = "workflow.image/upscale & detail";
    const discovery = "/gateway/v1/workflows/" + id;
    const fetcher = gapServe(() => Response.json(gapCatalog([gapRow({ id, discovery_url: discovery })])));
    gapMount(owner);
    const trigger = await screen.findByRole("button", { name: "Details for " + workflows[0].title });
    trigger.focus();
    await user.keyboard("{Enter}");
    let sheet = within(screen.getByRole("dialog"));
    const tryLink = sheet.getByRole("link", { name: "Open in Try" });
    expect(gapHref(tryLink).pathname.replace(/\/$/, "")).toBe("/try");
    expect(gapHref(tryLink).searchParams.get("workflow")).toBe(id);
    await user.click(sheet.getByText("Technical details", { exact: true }));
    await user.click(sheet.getByRole("button", { name: "Copy workflow ID" }));
    expect(await navigator.clipboard.readText()).toBe(id);
    expect(sheet.getByRole("link", { name: "Machine-readable workflow" })).toHaveAttribute("href", discovery);
    expect(sheet.getByRole("link", { name: "Discovery list" })).toHaveAttribute("href", "/me/api/workflows");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await user.click(trigger);
    sheet = within(screen.getByRole("dialog"));
    expect(sheet.getByRole("heading", { name: workflows[0].title })).toBeVisible();
    expect(gapCatalogCalls(fetcher).every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
  });

  // Protects page.tsx:158-167 at f1cac8a.
  // Mutant: Replace purposeLabel with an unconditional return of workflow.description.
  test.each([
    [{ purpose: "  A carefully trimmed purpose  ", description: "Fallback sentence." }, "A carefully trimmed purpose"],
    [{ purpose: "  ", description: "First sentence. Second sentence." }, "First sentence."],
    [{ purpose: "", description: "An engine binding is configured." }, null],
    [{ purpose: "", description: "" }, null],
  ] as Array<[Record<string, unknown>, string | null]>)(
    "G11 purpose uses explicit copy, first sentence, or no heading (%#)",
    async (patch, expected) => {
      const user = userEvent.setup();
      gapServe(() => Response.json(gapCatalog([gapRow(patch)])));
      gapMount();
      const sheet = await gapOpen(user);
      if (expected) {
        expect(sheet.getByRole("heading", { name: "Purpose" })).toBeVisible();
        expect(sheet.getByText(expected, { exact: true })).toBeVisible();
      } else {
        expect(sheet.queryByRole("heading", { name: "Purpose" })).toBeNull();
      }
      expect(sheet.queryByText("Second sentence.", { exact: true })).toBeNull();
      expect(sheet.queryByText("An engine binding is configured.", { exact: true })).toBeNull();
    },
  );

  // Protects page.tsx:229-236 at f1cac8a.
  // Mutant: Replace the type-switch result with the input's raw type string.
  test.each([
    ["boolean", "a yes-or-no choice"],
    ["array", "a list"],
    ["object", "structured information"],
    ["custom", "an input"],
  ])("G12 %s input has its declared-type noun and no optional summary", async (type, noun) => {
    const user = userEvent.setup();
    gapServe(() => Response.json(gapCatalog([gapRow({
      inputs: [{ name: "opaque", type, accepted: true, required: true }],
    })])));
    gapMount();
    const sheet = await gapOpen(user);
    expect(sheet.getByText("Needs: " + noun, { exact: true })).toBeVisible();
    expect(sheet.queryByText(/^Optional:/)).toBeNull();
  });

  // Protects page.tsx:272 at f1cac8a.
  // Mutant: Remove Set deduplication from outputSummary.
  test("G12 output names normalize and duplicate nouns are not repeated", async () => {
    const user = userEvent.setup();
    gapServe(() => Response.json(gapCatalog([gapRow({
      outputs: [
        { name: "first", label: "a result" },
        { name: "second", label: "a result" },
        { name: "STATUS_REPORT", media_type: "application/json" },
        { name: "" },
      ],
    })])));
    gapMount();
    const sheet = await gapOpen(user);
    expect(sheet.getByText("Makes: a result, a status report and an output")).toBeVisible();
  });

  // Protects page.tsx:264 at f1cac8a.
  // Mutant: Use every input rather than filtering by input.accepted for Needs.
  test("G12 summaries use accepted inputs while detail labels disambiguate all inputs", async () => {
    const user = userEvent.setup();
    gapServe(() => Response.json(gapCatalog([gapRow({
      inputs: [
        { name: "primary", type: "string", accepted: true, required: true },
        { name: "ignored", type: "string", accepted: false, required: true },
      ],
    })])));
    gapMount();
    const sheet = await gapOpen(user);
    expect(sheet.getByText("Needs: text", { exact: true })).toBeVisible();
    expect(sheet.queryByText(/^Optional:/)).toBeNull();
    await user.click(sheet.getByText("Input details", { exact: true }));
    const summaries = within(sheet.getByRole("list", { name: "Input summaries" }));
    expect(summaries.getByRole("heading", { name: "text for primary" })).toBeInTheDocument();
    const ignored = summaries.getByRole("heading", { name: "text for ignored" }).closest("li")!;
    expect(ignored).toHaveTextContent("Ignored");
  });

  // Protects page.tsx:115-120 at f1cac8a.
  // Mutant: Return a fixed time string rather than the numeric/null-specific label.
  test.each([
    [0, "~0 ms"],
    [0.125, "~125 ms"],
    [1, "~1.0 s"],
    [9, "~9.0 s"],
    [12, "~12 s"],
    [null, null],
    [undefined, null],
  ] as Array<[number | null | undefined, string | null]>)(
    "G13 typical time %s has its actual displayed unit",
    async (value, expected) => {
      const user = userEvent.setup();
      gapServe(() => Response.json(gapCatalog([gapRow({ typical_seconds: value })])));
      gapMount(owner);
      const sheet = await gapOpen(user);
      await user.click(sheet.getByText("Technical details", { exact: true }));
      if (expected) expect(sheet.getByText(expected, { exact: true })).toBeVisible();
      else expect(sheet.queryByText("Typical time", { exact: true })).toBeNull();
    },
  );

  // Protects page.tsx:492-500 at f1cac8a.
  // Mutant: Replace both missing input/output fact fallbacks with an empty string.
  test("G13 missing media and outputs have explicit detail fallbacks", async () => {
    const user = userEvent.setup();
    gapServe(() => Response.json(gapCatalog([gapRow({ inputs: [], outputs: null, aliases: [] })])));
    gapMount(owner);
    const sheet = await gapOpen(user);
    await user.click(sheet.getByText("Technical details", { exact: true }));
    const dialog = screen.getByRole("dialog");
    expect(gapFact(dialog, "Input media types")).toHaveTextContent("Not listed");
    expect(gapFact(dialog, "Output names and types")).toHaveTextContent("Not listed");
    expect(sheet.getByText("No other model names")).toBeVisible();
    expect(sheet.getByText("Makes: output not listed")).toBeVisible();
    expect(within(sheet.getByRole("list", { name: "Input summaries" })).queryByRole("listitem")).toBeNull();
  });

  // Protects page.tsx:147-155 at f1cac8a.
  // Mutant: Return declaredMediaTypes(input).join(", ") without plain format mapping or the empty fallback.
  test.each([
    [["image/png", "image/png", "image/jpeg"], "PNG or JPEG"],
    [["image/webp"], "WebP"],
    [["audio/wav"], "WAV"],
    [["audio/mpeg"], "MP3"],
    [["video/mp4"], "MP4"],
    [["application/pdf"], "PDF"],
    [["text/plain"], "plain text"],
    [["application/x-example"], "a file"],
    [[], "File formats not listed"],
  ] as Array<[string[], string]>)(
    "G13 member file formats %j have readable deduplicated labels",
    async (types, expected) => {
      const user = userEvent.setup();
      gapServe(() => Response.json(gapCatalog([gapRow({
        inputs: [{
          name: "source", type: "string", asset: true, label: "a source file",
          accepted: true, accepted_media_types: types,
        }],
      })])));
      gapMount();
      const sheet = await gapOpen(user);
      await user.click(sheet.getByText("Input details", { exact: true }));
      expect(gapFact(screen.getByRole("dialog"), "Accepted files"))
        .toHaveTextContent("a source file: " + expected);
    },
  );

  // Protects page.tsx:123-130 at f1cac8a.
  // Mutant: Replace rangeLabel with an unconditional empty string.
  test.each([
    [{ minimum: 2 }, "At least 2"],
    [{ maximum: 7 }, "At most 7"],
    [{ minimum: 2, maximum: 7 }, "2 to 7"],
    [{ enum: ["first", "second"], minimum: 2, maximum: 7 }, "first, second"],
    [{ enum: [], minimum: 2 }, "At least 2"],
  ] as Array<[Record<string, unknown>, string]>)(
    "G14 range branches appear in both table and narrow summary (%#)",
    async (range, expected) => {
      const user = userEvent.setup();
      gapServe(() => Response.json(gapCatalog([gapRow({
        inputs: [{ name: "control", type: "number", accepted: true, description: "Tuning control", ...range }],
      })])));
      gapMount(owner);
      const sheet = await gapOpen(user);
      await user.click(sheet.getByText("Technical details", { exact: true }));
      const table = within(sheet.getByLabelText("Scrollable inputs"));
      expect(table.getByRole("cell", { name: expected })).toBeInTheDocument();
      const summaries = within(sheet.getByRole("list", { name: "Input summaries" }));
      expect(summaries.getByText(expected, { exact: true })).toBeInTheDocument();
      expect(summaries.getByText("Tuning control")).toBeInTheDocument();
      expect(summaries.queryByText("Default", { exact: true })).toBeNull();
    },
  );

  // Protects page.tsx:140-142 at f1cac8a.
  // Mutant: Prefix plainValue's returned value with String(value), including null/objects.
  test.each([
    [false, "No"],
    [true, "Yes"],
    [0, "0"],
    ["plain", "plain"],
    [{ nested: true }, null],
    [[1, 2], null],
    [null, null],
  ] as Array<[unknown, string | null]>)(
    "G14 member defaults show only plain values (%#)",
    async (value, expected) => {
      const user = userEvent.setup();
      gapServe(() => Response.json(gapCatalog([gapRow({
        inputs: [{ name: "choice", label: "Choice",
          type: Array.isArray(value) ? "array" : value === null ? "string" : typeof value,
          accepted: true, default: value }],
      })])));
      gapMount();
      const sheet = await gapOpen(user);
      await user.click(sheet.getByText("Input details", { exact: true }));
      const summaries = within(sheet.getByRole("list", { name: "Input summaries" }));
      const item = summaries.getByRole("heading", { name: "Choice" }).closest("li")!;
      if (expected !== null) {
        expect(within(item).getByText("Default", { exact: true }).nextElementSibling).toHaveTextContent(expected);
      } else expect(within(item).queryByText("Default", { exact: true })).toBeNull();
      expect(within(item).queryByText("Range", { exact: true })).toBeNull();
      const table = within(sheet.getByLabelText("Scrollable inputs"));
      const row = table.getAllByRole("row")[1];
      expect(within(row).getAllByRole("cell")[2].textContent).toBe(expected ?? "");
      expect(within(row).getAllByRole("cell")[3].textContent).toBe("");
    },
  );

  // Protects page.tsx:145 at f1cac8a.
  // Mutant: Replace enum.map(plainValue).filter(Boolean) with enum.map(String).
  test("G14 member boolean enums use Yes/No and omit object entries", async () => {
    const user = userEvent.setup();
    gapServe(() => Response.json(gapCatalog([gapRow({
      inputs: [{
        name: "choice", label: "Choice", type: "boolean", accepted: true,
        enum: [true, false, { opaque: true }],
      }],
    })])));
    gapMount();
    const sheet = await gapOpen(user);
    await user.click(sheet.getByText("Input details", { exact: true }));
    expect(within(sheet.getByLabelText("Scrollable inputs"))
      .getByRole("cell", { name: "Yes, No" })).toBeInTheDocument();
    expect(screen.getByRole("dialog")).not.toHaveTextContent("[object Object]");
  });

  // Protects page.tsx:716 at f1cac8a.
  // Mutant: Remove limit: "5" from the recent-creations URL parameters.
  test("G15 recent creations load independently with exact workflow, limit and credentials", async () => {
    const user = userEvent.setup();
    const pending = gapDeferred<Response>();
    const fetcher = gapServe(undefined, () => pending.promise);
    gapMount();
    const sheet = await gapOpen(user);
    expect(sheet.getByRole("status", { name: "Loading recent creations" })).toBeVisible();
    expect(sheet.queryByText("No creations yet. Open this workflow in Try.")).toBeNull();
    const requests = gapMediaCalls(fetcher);
    expect(requests).toHaveLength(1);
    const [path, init] = requests[0];
    const url = new URL(path, "https://example.test");
    expect(url.pathname).toBe("/me/api/media");
    expect([...url.searchParams.entries()]).toEqual([
      ["workflow", workflows[0].id],
      ["limit", "5"],
    ]);
    expect(init).toEqual({ credentials: "same-origin" });
    await act(async () => { pending.resolve(Response.json({ assets: [] })); });
    expect(await sheet.findByText("No creations yet. Open this workflow in Try.")).toBeVisible();
    expect(sheet.queryByRole("status", { name: "Loading recent creations" })).toBeNull();
    expect(gapCatalogCalls(fetcher)).toHaveLength(1);
  });

  // Protects page.tsx:737 at f1cac8a.
  // Mutant: Replace the recent-creations onRetry callback with a no-op.
  test.each([
    ["HTTP", () => new Response(null, { status: 503 })],
    ["shape", () => Response.json({ assets: null })],
    ["network", () => Promise.reject(new TypeError("Network unavailable"))],
    ["JSON", () => new Response("{", { headers: { "content-type": "application/json" } })],
  ] as Array<[string, () => Response | Promise<Response>]>)(
    "G16 recent %s error retries only media and recovers",
    async (_name, first) => {
      const user = userEvent.setup();
      let attempt = 0;
      const pending = gapDeferred<Response>();
      const fetcher = gapServe(undefined, () => ++attempt === 1 ? first() : pending.promise);
      gapMount();
      const sheet = await gapOpen(user);
      expect(await sheet.findByText(gapMediaError)).toBeVisible();
      expect(sheet.getByRole("link", { name: "Open in Try" })).toBeVisible();
      await user.click(sheet.getByRole("button", { name: "Try again" }));
      await waitFor(() => expect(gapMediaCalls(fetcher)).toHaveLength(2));
      await act(async () => {
        pending.resolve(Response.json({ assets: [gapAsset({ prompt: "Recovered creation" })] }));
      });
      expect(await sheet.findByText("Recovered creation")).toBeVisible();
      expect(sheet.queryByText(gapMediaError)).toBeNull();
      expect(gapCatalogCalls(fetcher)).toHaveLength(1);
      expect(gapMediaCalls(fetcher)[1]).toEqual(gapMediaCalls(fetcher)[0]);
    },
  );

  // Protects page.tsx:764 at f1cac8a.
  // Mutant: Render the asset facts dl even when model, workflow and generation_ms are absent.
  test("G17 nullable asset metadata renders the minimal real assets shape", async () => {
    const user = userEvent.setup();
    gapServe(undefined, () => Response.json({ assets: [gapAsset()] }));
    gapMount();
    const sheet = await gapOpen(user);
    const recent = within(sheet.getByRole("region", { name: "Recent creations" }));
    expect(await recent.findByText("Image output")).toBeVisible();
    const library = recent.getByRole("link", { name: "Open in Library" });
    expect(gapHref(library).pathname.replace(/\/$/, "")).toBe("/library");
    expect(gapHref(library).searchParams.get("asset")).toBe(gapAssetId);
    for (const label of ["Model", "Workflow", "Duration", "Technical details"]) {
      expect(recent.queryByText(label, { exact: true })).toBeNull();
    }
    expect(recent.queryByRole("link", { name: "View receipt" })).toBeNull();
    expect(recent.queryByRole("link", { name: "Download output" })).toBeNull();
    expect(recent.getByRole("listitem").querySelector("dl")).toBeNull();
    expect(recent.getByRole("listitem")).not.toHaveTextContent(/Invalid Date|NaN/);
  });

  // Protects page.tsx:778 at f1cac8a.
  // Mutant: Check asset.generation_ms for truthiness instead of != null.
  test.each([member, owner])("G17 populated assets keep exact links and role-gated raw model for $role", async (viewer) => {
    const user = userEvent.setup();
    const requestId = "request/example & 1";
    const download = "/outputs/example.png?download=1";
    gapServe(undefined, () => Response.json({
      assets: [gapAsset({
        kind: "audio",
        prompt: "An example narration",
        model: "example-model-raw",
        workflow: workflows[0].id,
        generation_ms: 0,
        request_id: requestId,
        download_url: download,
      })],
    }));
    gapMount(viewer);
    const sheet = await gapOpen(user);
    const region = sheet.getByRole("region", { name: "Recent creations" });
    const recent = within(region);
    expect(await recent.findByText("Audio output")).toBeVisible();
    expect(recent.getByText("An example narration")).toBeVisible();
    expect(recent.getByText("0.0 s")).toBeVisible();
    expect(recent.getByText("Model", { exact: true })).toBeVisible();
    expect(recent.getByText("Workflow", { exact: true })).toBeVisible();
    expect(recent.getByText(workflows[0].title, { exact: true })).toBeVisible();
    if (viewer.role !== "owner") expect(recent.queryByText(workflows[0].id, { exact: true })).toBeNull();
    const receipt = recent.getByRole("link", { name: "View receipt" });
    expect(gapHref(receipt).pathname.replace(/\/$/, "")).toBe("/usage");
    expect(gapHref(receipt).searchParams.get("receipt")).toBe(requestId);
    expect(recent.getByRole("link", { name: "Download output" }))
      .toHaveAttribute("href", download);
    expect(recent.getByRole("link", { name: "Download output" }))
      .toHaveAttribute("download");
    if (viewer.role === "owner") {
      const summary = recent.getByText("Technical details", { exact: true });
      const disclosure = summary.closest("details")!;
      expect(disclosure).not.toHaveAttribute("open");
      await user.click(summary);
      expect(within(disclosure).getByText("example-model-raw", { exact: true })).toBeVisible();
      expect(within(disclosure).getByText(workflows[0].id, { exact: true })).toBeVisible();
    } else {
      expect(recent.queryByText("Technical details", { exact: true })).toBeNull();
      expect(region.querySelector("details")).toBeNull();
    }
  });

  // Protects page.tsx:764 at f1cac8a.
  // Mutant: Join the three metadata conditions with && instead of ||.
  test.each([
    [{ workflow: workflows[0].id }, "Workflow"],
    [{ generation_ms: 1500 }, "Duration"],
    [{ model: "example-model" }, "Model"],
  ] as Array<[Record<string, unknown>, string]>)(
    "G17 an isolated %s metadata field still creates the correct fact",
    async (patch, label) => {
      const user = userEvent.setup();
      gapServe(undefined, () => Response.json({ assets: [gapAsset(patch)] }));
      gapMount();
      const sheet = await gapOpen(user);
      const recent = within(sheet.getByRole("region", { name: "Recent creations" }));
      expect(await recent.findByText(label, { exact: true })).toBeVisible();
      for (const other of ["Model", "Workflow", "Duration"].filter((item) => item !== label)) {
        expect(recent.queryByText(other, { exact: true })).toBeNull();
      }
    },
  );

  // Protects page.tsx:727 at f1cac8a.
  // Mutant: Show the recent-creations loading skeleton for isFetching instead of isPending.
  test("G18 media background refresh retains old data, then reports failure and retries", async () => {
    const user = userEvent.setup();
    const pending = gapDeferred<Response>();
    let attempt = 0;
    gapServe(undefined, () => {
      attempt += 1;
      if (attempt === 1) return Response.json({ assets: [gapAsset({ prompt: "Previous creation" })] });
      if (attempt === 2) return pending.promise;
      return Response.json({ assets: [gapAsset({ prompt: "Latest creation" })] });
    });
    const { client } = gapMount();
    const sheet = await gapOpen(user);
    expect(await sheet.findByText("Previous creation")).toBeVisible();
    const done = gapStartRefresh(client, ["workflow-media", workflows[0].id]);
    expect(sheet.getByText("Previous creation")).toBeVisible();
    expect(sheet.queryByRole("status", { name: "Loading recent creations" })).toBeNull();
    await act(async () => {
      pending.resolve(Response.json({}, { status: 503 }));
      await done;
    });
    expect(await sheet.findByText(gapMediaError)).toBeVisible();
    expect(sheet.queryByText("Previous creation")).toBeNull();
    await user.click(sheet.getByRole("button", { name: "Try again" }));
    expect(await sheet.findByText("Latest creation")).toBeVisible();
    expect(sheet.queryByText(gapMediaError)).toBeNull();
  });

  // Protects page.tsx:713 at f1cac8a.
  // Mutant: Remove workflow.id from the workflow-media query key.
  test("G19 a late response from a closed workflow cannot replace another workflow's creations", async () => {
    const user = userEvent.setup();
    const first = gapDeferred<Response>();
    const second = gapDeferred<Response>();
    const mediaPaths: string[] = [];
    const fetcher = vi.fn(async (url: string, _init?: RequestInit) => {
      if (url.includes("/me/api/media")) {
        mediaPaths.push(url);
        const id = new URL(url, "https://example.test").searchParams.get("workflow");
        if (id === workflows[0].id) return first.promise;
        if (id === workflows[1].id) return second.promise;
        throw new Error("Unexpected workflow in media request");
      }
      return url === "/me/api/workflows"
        ? Response.json(gapCatalog([gapRow(), workflows[1]]))
        : new Response(null, { status: 404 });
    });
    vi.stubGlobal("fetch", fetcher);
    const { client } = gapMount();
    let sheet = await gapOpen(user);
    expect(sheet.getByRole("status", { name: "Loading recent creations" })).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    sheet = await gapOpen(user, workflows[1].title);
    expect(sheet.getByRole("status", { name: "Loading recent creations" })).toBeVisible();
    await act(async () => {
      second.resolve(Response.json({
        assets: [gapAsset({ asset_id: "img_" + "b".repeat(64), prompt: "Second workflow creation" })],
      }));
    });
    expect(await sheet.findByText("Second workflow creation")).toBeVisible();
    await act(async () => {
      first.resolve(Response.json({ assets: [gapAsset({ prompt: "Late first workflow creation" })] }));
    });
    await waitFor(() => expect(client.getQueryState(["workflow-media", workflows[0].id])?.status).toBe("success"));
    expect(sheet.queryByText("Late first workflow creation")).toBeNull();
    expect(sheet.getByText("Second workflow creation")).toBeVisible();
    expect(sheet.getByRole("heading", { name: workflows[1].title })).toBeVisible();
    expect(mediaPaths.map((path) => new URL(path, "https://example.test").searchParams.get("workflow")))
      .toEqual([workflows[0].id, workflows[1].id]);
  });

  // Protects page.tsx:715-717 at f1cac8a.
  // Mutant: Resolve RecentJobs from workflow.recent_jobs instead of readRecentAssets.
  test.each(["queued", "running", "cancelled"])(
    "G20 catalog recent_jobs status %s is metadata, not a fabricated runner state",
    async (status) => {
      const user = userEvent.setup();
      const fetcher = gapServe(() => Response.json(gapCatalog([gapRow({
        recent_jobs: [{
          job_id: "example-job-id", status, created_at: 1700000000,
          updated_at: 1700000001, latency_ms: 1000,
        }],
      })])));
      gapMount();
      const sheet = await gapOpen(user);
      expect(await sheet.findByText("No creations yet. Open this workflow in Try.")).toBeVisible();
      expect(sheet.queryByText("example-job-id", { exact: true })).toBeNull();
      expect(sheet.queryByText(status, { exact: true })).toBeNull();
      expect(sheet.queryByRole("button", { name: /^cancel/i })).toBeNull();
      expect(sheet.queryByRole("textbox")).toBeNull();
      const navigate = sheet.getByRole("link", { name: "Open in Try" });
      navigate.addEventListener("click", (event) => event.preventDefault(), { once: true });
      await user.click(navigate);
      expect(gapHref(navigate).searchParams.get("workflow")).toBe(workflows[0].id);
      expect(fetcher.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
    },
  );

  const badCatalogs: Array<[string, unknown]> = [
    ["null envelope", null],
    ["array envelope", []],
    ["boolean envelope", false],
    ["wrong version", { schema_version: 2, workflows: [] }],
    ["string version", { schema_version: "1", workflows: [] }],
    ["missing version", { workflows: [] }],
    ["missing workflow list", { schema_version: 1 }],
    ["object workflow list", { schema_version: 1, workflows: {} }],
    ["null workflow row", gapCatalog([null])],
    ["array workflow row", gapCatalog([[]])],
    ["missing inputs", gapCatalog([gapRow({ inputs: undefined })])],
    ["object inputs", gapCatalog([gapRow({ inputs: {} })])],
    ["null input", gapCatalog([gapRow({ inputs: [null] })])],
    ["object outputs", gapCatalog([gapRow({ outputs: {} })])],
    ["null output row", gapCatalog([gapRow({ outputs: [null] })])],
    ["null aliases", gapCatalog([gapRow({ aliases: null })])],
    ["non-string alias", gapCatalog([gapRow({ aliases: ["valid", 4] })])],
    ["nonfinite typical time", gapCatalog([gapRow({ typical_seconds: Infinity })])],
    ["null availability", gapCatalog([gapRow({ availability: null })])],
    ["array availability", gapCatalog([gapRow({ availability: [] })])],
    ["missing status", gapCatalog([gapRow({ availability: {} })])],
    ["non-string reason", gapCatalog([gapRow({ availability: { status: "ready", reason_code: 7 } })])],
    ["null examples", gapCatalog([gapRow({ examples: null })])],
    ["array examples", gapCatalog([gapRow({ examples: [] })])],
  ];
  for (const key of ["id", "capability", "title", "description"]) {
    badCatalogs.push(["required row string " + key, gapCatalog([gapRow({ [key]: null })])]);
  }
  for (const key of ["purpose", "technical_notes", "engine_binding", "native_route", "try_url", "discovery_url"]) {
    badCatalogs.push(["optional row string " + key, gapCatalog([gapRow({ [key]: 7 })])]);
  }
  const validInput = { name: "text", type: "string", accepted: true };
  for (const [key, value] of [
    ["name", null], ["type", 7], ["accepted", undefined], ["accepted", "true"],
    ["required", "true"], ["asset", 1], ["label", {}], ["description", []],
    ["media_type", 4], ["accepted_media_types", "image/png"], ["accepted_media_types", ["image/png", null]],
    ["minimum", "0"], ["maximum", "1"], ["minimum", Infinity], ["maximum", NaN], ["enum", {}],
  ] as Array<[string, unknown]>) {
    badCatalogs.push(["input " + key + " " + String(value), gapCatalog([
      gapRow({ inputs: [{ ...validInput, [key]: value }] }),
    ])]);
  }
  for (const key of ["name", "label", "media_type", "description"]) {
    badCatalogs.push(["output " + key, gapCatalog([gapRow({ outputs: [{ name: "out", [key]: 4 }] })])]);
  }
  for (const key of ["available", "measured", "configured", "enabled", "up", "idle_startable"]) {
    badCatalogs.push(["availability flag " + key, gapCatalog([
      gapRow({ availability: { status: "ready", [key]: "true" } }),
    ])]);
  }
  for (const key of ["curl", "python", "javascript"]) {
    badCatalogs.push(["example " + key, gapCatalog([
      gapRow({ examples: { ...workflows[0].examples, [key]: 7 } }),
    ])]);
  }

  // Protects workflow-api.ts:115-117 at f1cac8a.
  // Mutant: Return true from isWorkflowCatalog for the malformed payload named by the case.
  test.each(badCatalogs)("G21 catalog guard rejects %s", (_name, payload) => {
    // Infinity/NaN cases exercise the direct guard, not a fictitious JSON encoding.
    expect(isWorkflowCatalog(payload)).toBe(false);
  });

  const optionalRowFields = [
    "purpose", "technical_notes", "engine_binding", "native_route",
    "try_url", "discovery_url", "typical_seconds", "outputs",
  ];
  const optionalInputFields = [
    "required", "asset", "label", "description", "media_type",
    "accepted_media_types", "minimum", "maximum", "enum",
  ];
  // Protects workflow-api.ts:84-86,109 at f1cac8a.
  // Mutant: Require optional values to have their scalar/array type even when null or absent.
  test.each([undefined, null])("G21 optional fields accept %s consistently", (empty) => {
    const row: Record<string, unknown> = gapRow();
    for (const key of optionalRowFields) row[key] = empty;
    const input: Record<string, unknown> = { ...validInput };
    for (const key of optionalInputFields) input[key] = empty;
    row.inputs = [input];
    row.availability = {
      status: "ready", reason_code: empty, available: empty, measured: empty,
      configured: empty, enabled: empty, up: empty, idle_startable: empty,
    };
    expect(isWorkflowCatalog(gapCatalog([row]))).toBe(true);
  });

  // Protects workflow-api.ts:108-109 at f1cac8a.
  // Mutant: Require aliases.length > 0, rejecting the valid empty aliases array.
  test("G21 guard accepts empty collections, boundary values and extra gateway catalog fields", () => {
    expect(isWorkflowCatalog(gapCatalog([]))).toBe(true);
    const payload = {
      ...gapCatalog([gapRow({
        aliases: [],
        outputs: [{ name: "out", label: null, media_type: null, description: null }],
        inputs: [{
          name: "control", type: "number", accepted: false, required: false, asset: false,
          minimum: 0, maximum: 1, enum: [], default: { preserved: true },
          accepted_media_types: [],
        }],
        typical_seconds: 0,
        availability: {
          status: "configured", reason_code: "configured", available: false, measured: true,
          configured: false, enabled: false, up: false, idle_startable: false,
        },
        recent_jobs: [{ job_id: "job-example", status: "running" }],
      })]),
      object: "workflow.catalog",
      groups: [],
      counts: { workflows: 1, capabilities: 1 },
      links: { self: "/me/api/workflows" },
    };
    expect(isWorkflowCatalog(payload)).toBe(true);
  });

  // Protects workflow-api.ts:70 at f1cac8a.
  // Mutant: Remove credentials: "same-origin" from readJson's fetch call.
  test("G22 readJson preserves successful JSON and same-origin request options", async () => {
    const payload = { arbitrary: ["a"], nested: { untouched: true } };
    const fetcher = vi.fn(async () => Response.json(payload));
    vi.stubGlobal("fetch", fetcher);
    expect(await readJson<typeof payload>("/me/api/media?workflow=example&limit=5")).toEqual(payload);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith(
      "/me/api/media?workflow=example&limit=5", { credentials: "same-origin" },
    );
  });

  // Protects workflow-api.ts:71-72 at f1cac8a.
  // Mutant: Delete the non-OK response check from readJson.
  test("G22 readJson uses its own message for non-OK HTTP responses", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      error: { message: "Do not use the server message as this helper's oracle" },
    }, { status: 403 })));
    await expect(readJson("/me/api/media")).rejects.toThrow("Workflow data could not be loaded. Try again.");
  });

  // Protects workflow-api.ts:70-73 at f1cac8a.
  // Mutant: Catch a network/JSON rejection and resolve {} instead.
  test("G22 readJson preserves network rejection and rejects invalid JSON", async () => {
    const networkError = new TypeError("offline");
    vi.stubGlobal("fetch", vi.fn(async () => { throw networkError; }));
    await expect(readJson("/me/api/media")).rejects.toBe(networkError);
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response("{", { headers: { "content-type": "application/json" } }),
    ));
    await expect(readJson("/me/api/media")).rejects.toThrow();
  });

  const badMedia: Array<[string, unknown]> = [
    ["null envelope", null],
    ["array envelope", []],
    ["missing assets", {}],
    ["jobs envelope is not assets", { jobs: [] }],
    ["object assets", { assets: {} }],
    ["null assets", { assets: null }],
    ["null asset row", { assets: [null] }],
    ["array asset row", { assets: [[]] }],
    ["non-string id", { assets: [gapAsset({ asset_id: 2 })] }],
    ["non-string kind", { assets: [gapAsset({ kind: 2 })] }],
    ["non-number duration", { assets: [gapAsset({ generation_ms: "0" })] }],
    ["boolean timestamp", { assets: [gapAsset({ created_at: false })] }],
    ["object timestamp", { assets: [gapAsset({ created_at: {} })] }],
  ];
  for (const key of ["prompt", "model", "job_id", "workflow", "download_url", "request_id"]) {
    badMedia.push(["optional string " + key, { assets: [gapAsset({ [key]: 4 })] }]);
  }
  // Protects workflow-api.ts:129 at f1cac8a.
  // Mutant: Remove the isRecentAssets rejection from readRecentAssets.
  test.each(badMedia)("G23 readRecentAssets rejects %s", async (_name, payload) => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(payload)));
    await expect(readRecentAssets("/me/api/media")).rejects.toThrow(gapMediaError);
  });

  // Protects workflow-api.ts:130 at f1cac8a.
  // Mutant: Return { assets: [] } instead of the accepted payload.
  test.each([
    { assets: [] },
    { assets: [{ asset_id: gapAssetId, kind: "image" }] },
    { assets: [gapAsset()] },
    { assets: [gapAsset({ job_id: null, request_id: null, download_url: null, generation_ms: null })] },
    { assets: [gapAsset({ created_at: 1700000000, generation_ms: 0 })] },
    { assets: [gapAsset({ created_at: "2026-10-13T12:00:00Z" })] },
    { assets: [gapAsset({ created_at: "unparsed timestamp", job_id: null })] },
    {
      assets: [gapAsset({
        job_id: "job-example", request_id: "request-example", download_url: "/opaque-download",
        workflow: workflows[0].id, model: "example-model", prompt: "Example output",
        generation_ms: 1250,
      })],
      models: [],
      next_cursor: null,
    },
  ])("G23 readRecentAssets returns accepted payload %# unchanged", async (payload) => {
    // Timestamp strings are shape-checked, not parsed by this helper.
    const fetcher = vi.fn(async () => Response.json(payload));
    vi.stubGlobal("fetch", fetcher);
    expect(await readRecentAssets("/me/api/media")).toEqual(payload);
    expect(fetcher).toHaveBeenCalledWith("/me/api/media", { credentials: "same-origin" });
  });

  // Protects workflow-api.ts:71-72 at f1cac8a.
  // Mutant: Replace readJson's HTTP error with the malformed recent-creations error.
  test("G23 readRecentAssets preserves the HTTP failure distinct from malformed shape", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ assets: [] }, { status: 503 })));
    await expect(readRecentAssets("/me/api/media")).rejects.toThrow(
      "Workflow data could not be loaded. Try again.",
    );
  });
});

test("recent creations omit unmatched workflow names for members", async () => {
  const user = userEvent.setup();
  gapServe(undefined, () => Response.json({ assets: [gapAsset({ workflow: "unknown-private-workflow" })] }));
  gapMount();
  const sheet = await gapOpen(user);
  const recent = within(sheet.getByRole("region", { name: "Recent creations" }));
  await recent.findByText("Image output");
  expect(recent.queryByText("Workflow", { exact: true })).toBeNull();
  expect(recent.queryByText("unknown-private-workflow")).toBeNull();
});
