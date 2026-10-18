import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "@/lib/api";
import {
  enginesApi,
  type EngineRecordV2,
  type EngineView,
  type TransformRow,
} from "@/lib/engines-api";
import {
  accessAnswer,
  definitions,
  engineView,
  renderWithClient,
  testResult,
  check as checkFixture,
} from "../__fixtures__/test-utils";
import { EngineRecordPage } from "./engine-record-page";
import { ConnectionFacts, OfferingFacts, ObservationList, ProcessFacts, ResourceFacts, UnsupportedNote } from "./contract-sections";

const viewer = vi.hoisted(() => ({
  current: {
    authenticated: true,
    name: "Tim",
    email: null,
    role: "owner",
    admin: true,
  },
}));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => viewer.current,
}));

const registry = { schema_version: 1, services: {} } as never;
const OWNER_ONLY = "Only the owner can change this.";

/** The v2 record the gateway sends with the view (gateway/service_registry.py service_record_v2). */
function v2(over: Partial<EngineRecordV2> = {}): EngineRecordV2 {
  return {
    schema_version: 2,
    id: "garage-gpu",
    name: "garage-gpu",
    enabled: true,
    record_type: "engine",
    derived: false,
    editable: true,
    provenance: { source: "ui", package: null },
    connection: {
      transport: "http",
      address: "http://192.168.1.50:8080/v1",
      protocol: "openai-compatible",
      credentials: {
        inference: {
          secret_ref: "configured",
          env: null,
          origin: "http://192.168.1.50:8080",
          header: "Authorization",
          scheme: "Bearer",
        },
        control: null,
      },
      headers: [],
      timeouts: { total_s: 600 },
    },
    offerings: [
      {
        id: "qwen3-32b",
        kind: "model",
        upstream: "qwen3-32b-instruct-q4_k_m",
        selection: { mode: "request_field", implicit_load: "unknown" },
        tasks: [{ task: "chat", evidence: "declared" }],
      },
    ],
    process: { driver: "none" },
    observations: {},
    residency: {},
    resources: {},
    ...over,
  };
}

function section(heading: string): HTMLElement {
  return screen
    .getByRole("heading", { level: 2, name: heading })
    .closest("section") as HTMLElement;
}

function mount(over: Partial<EngineView> = {}) {
  const view = engineView({
    v2: v2(),
    request_path: {
      mode: "direct",
      source: "service record",
      url: "http://192.168.1.50:8080/v1/chat/completions",
    },
    credential_destination: "http://192.168.1.50:8080",
    ...over,
  });
  vi.spyOn(enginesApi, "view").mockImplementation(async () =>
    structuredClone(view),
  );
  renderWithClient(
    <EngineRecordPage
      serviceId={view.service_id}
      onBack={() => undefined}
      onRemove={() => undefined}
    />,
  );
  return view;
}

beforeEach(() => {
  viewer.current = {
    authenticated: true,
    name: "Tim",
    email: null,
    role: "owner",
    admin: true,
  };
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(
    structuredClone(definitions),
  );
  vi.spyOn(enginesApi, "access").mockResolvedValue(accessAnswer("nobody"));
});
afterEach(() => cleanup());

describe("the six sections mirror the record", () => {
  it("an owner rename saves metadata without rebuilding a legacy connection", async () => {
    mount();
    const edit = vi.spyOn(api.services, "edit").mockResolvedValue(registry);
    const update = vi.spyOn(enginesApi, "update");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Name"), "!");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(edit).toHaveBeenCalledWith("garage-gpu", { name: "garage-gpu!" }));
    expect(update).not.toHaveBeenCalled();
  });

  it("explains why stored credential headers are hidden", async () => {
    mount({ ignored_headers: ["Authorization"] });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    expect(screen.getByTestId("connection-facts")).toHaveTextContent(
      "Headers not sentAuthorization. These stored headers cannot override credentials or HTTP transport settings.",
    );
  });

  it("preserves structured offering declarations and explains their read-only editor", async () => {
    mount({
      can_edit_offerings: false,
      offering_edit_reason: "These offerings declare selection or workflow settings that the model-list editor cannot preserve. They are shown as stored.",
      v2: v2({ offerings: [{ id: "whole-workflow", kind: "workflow", selection: { mode: "whole_graph" } }] }),
    });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    const offers = section("What it offers");
    expect(within(offers).getByText(/the model-list editor cannot preserve/)).toBeVisible();
    expect(within(offers).getByTestId("offering-facts")).toHaveTextContent("Each request runs the whole workflow.");
    expect(within(offers).queryByLabelText("Name apps use")).toBeNull();
    expect(screen.getByLabelText("Address")).toBeEnabled();
    for (const button of within(offers).queryAllByRole("button")) expect(button).toBeDisabled();
  });

  it("a connection save omits read-only offering adjustments even when the serializer fills defaults", async () => {
    mount({
      can_edit_offerings: false,
      offering_edit_reason: "Structured offerings are shown as stored.",
      // Legacy records can omit role; the current serializer fills it.
      values: { ...engineView().values, transforms: [{ kind: "message-inject", text: "Be brief." } as TransformRow] },
    });
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    const user = userEvent.setup();
    await user.click(await screen.findByText("More options", { selector: "summary span" }));
    await user.clear(screen.getByLabelText("Time to wait for a reply"));
    await user.type(screen.getByLabelText("Time to wait for a reply"), "37");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    const body = update.mock.calls[0][1];
    expect(body.values).toMatchObject({ reply_timeout_s: 37 });
    expect(body.values).not.toHaveProperty("transforms");
    expect(body).not.toHaveProperty("models");
    expect(body).not.toHaveProperty("capabilities");
  });

  it("direct: shows the effective request path, the key's destination, the offering and an unknown observation", async () => {
    mount();
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    for (const heading of [
      "Connection",
      "What it offers",
      "How it runs",
      "What Azizam can check",
      "Resources",
      "Stored record",
    ])
      expect(
        screen.getByRole("heading", { level: 2, name: heading }),
      ).toBeInTheDocument();
    const facts = screen.getByTestId("connection-facts");
    expect(facts).toHaveTextContent(
      "Saved request destinationshttp://192.168.1.50:8080/v1/chat/completions",
    );
    expect(facts).toHaveTextContent(
      "Azizam sends requests directly to this address. From this engine’s saved connection settings.",
    );
    expect(facts).toHaveTextContent(
      "Inference key is sent tohttp://192.168.1.50:8080 only.",
    );
    expect(screen.getByLabelText("Address")).toBeEnabled();
    expect(screen.queryByText(OWNER_ONLY)).toBeNull();
    expect(screen.queryByText(/can’t be changed here/)).toBeNull();
    const offers = section("What it offers");
    expect(within(offers).getByTestId("offering-facts")).toHaveTextContent(
      "Apps choose this model with the request’s model field.",
    );
    expect(within(offers).getByText(/Sent as/)).toHaveTextContent(
      "Sent as qwen3-32b-instruct-q4_k_m",
    );
    expect(
      within(section("How it runs")).getByTestId("process-facts"),
    ).toHaveTextContent(/Who runs itNot Azizam/);
    // An external server has no start and stop commands to fill in.
    expect(screen.queryByLabelText("Start command")).toBeNull();
    const checks = within(section("What Azizam can check")).getByTestId(
      "observations",
    );
    expect(
      within(checks).getByText("The server is up").parentElement,
    ).toHaveTextContent(
      "No check is configured, so this is unknown.",
    );
    expect(
      within(checks).getAllByLabelText("Unknown").length,
    ).toBeGreaterThanOrEqual(6);
    expect(
      within(section("Resources")).getByText("None recorded."),
    ).toBeInTheDocument();
  });

  it("hop + process: the request goes through the gateway's hop, the outside manager owns the process and its system file is read-only", async () => {
    mount({
      lifecycle_editable: false,
      request_path: {
        mode: "gateway_lifecycle",
        source: "service record",
        url: "http://127.0.0.1:8081/v1/chat/completions",
      },
      v2: v2({
        process: {
          driver: "command",
          start: ["/opt/llm/server", "--model", "/models/My Model.gguf"],
          stop: ["systemctl", "--user", "stop", "local-llm"],
          status: ["systemctl", "--user", "is-active", "local-llm"],
          profile_apply: {
            kind: "write_env_file_then_restart",
            file: "~/.config/systemd/user/local-llm.service.d/95-azizam.conf",
            reload: ["systemctl", "--user", "daemon-reload"],
            restore: "on_failure",
          },
        },
        observations: {
          process_ready: {
            http: { path: "/health", expect: 200 },
            last: { ok: true, detail: "200 OK" },
          },
        },
        offerings: [
          {
            id: "a",
            kind: "model",
            selection: { mode: "switch", switch: { call: "switch" } },
          },
          {
            id: "b",
            kind: "model",
            selection: { mode: "switch", switch: { call: "switch" } },
          },
        ],
        selected_offering: "b",
        resources: {
          gpu: { devices: [0], vram_gb_estimate: 18, enforced: false },
        },
      }),
    });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    expect(screen.getByTestId("connection-facts")).toHaveTextContent(
      "http://127.0.0.1:8081/v1/chat/completionsAzizam starts the server when needed, then forwards the request to this address.",
    );
    // Hop engines are no longer refused: the connection edits like any other.
    expect(screen.getByLabelText("Address")).toBeEnabled();
    expect(
      within(section("What it offers")).getByTestId("offering-facts"),
    ).toHaveTextContent("Selected now: b");
    const runs = section("How it runs");
    const process = within(runs).getByTestId("process-facts");
    expect(process).toHaveTextContent(
      "An outside process manager owns this server. Azizam asks it to start and stop using the saved commands.",
    );
    // The full argument list, with the argument boundaries visible.
    expect(process).toHaveTextContent('/opt/llm/server --model "/models/My Model.gguf"');
    expect(process).toHaveTextContent("systemctl --user stop local-llm");
    expect(process).toHaveTextContent(
      "~/.config/systemd/user/local-llm.service.d/95-azizam.conf",
    );
    expect(process).toHaveTextContent("The saved process settings choose this path.");
    expect(within(runs).queryByRole("textbox")).toBeNull();
    expect(
      within(runs).queryByLabelText("Who starts and stops this server"),
    ).toBeNull();
    expect(
      within(runs).getByText(
        /^This page cannot safely edit this server’s process setup yet/,
      ),
    ).toBeVisible();
    const checks = within(section("What Azizam can check")).getByTestId(
      "observations",
    );
    expect(
      within(checks).getByText("The server is up").parentElement,
    ).toHaveTextContent("Asks GET /health, expects 200. Saved result: 200 OK.");
    expect(
      within(checks)
        .getByText("The server is up")
        .closest("li")
        ?.querySelector('[aria-label="Verified by a check"]'),
    ).not.toBeNull();
    expect(
      within(section("Resources")).getByTestId("resource-facts"),
    ).toHaveTextContent(
      "Devices: [0]; Estimated video memory (GB): 18. Resource estimate only; not enforced.",
    );
  });

  it("substrate: only the system record is locked, with its derived source and reason", async () => {
    mount({
      locked: true,
      editable: false,
      v2: v2({
        record_type: "system",
        derived: true,
        editable: false,
        process: null,
        offerings: [],
      }),
    });
    expect(
      await screen.findByText(
        "Azizam runs this engine itself. Only its name and Enabled can change.",
      ),
    ).toBeVisible();
    expect(
      screen.getByText(
        /Azizam creates these settings from your public model names and engines/,
      ),
    ).toBeVisible();
    expect(
      screen.getByText("Source: derived by Azizam (system record)."),
    ).toBeVisible();
    expect(screen.getByLabelText("Address")).toBeDisabled();
    expect(screen.getByLabelText("Name")).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "Find new models" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove engine" })).toBeNull();
    const edit = vi.spyOn(api.services, "edit").mockResolvedValue(registry);
    const update = vi.spyOn(enginesApi, "update");
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Name"), "!");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() =>
      expect(edit).toHaveBeenCalledWith("garage-gpu", { name: "garage-gpu!" }),
    );
    expect(update).not.toHaveBeenCalled();
  });

  it("administrator: every owner-only part is read-only with the exact words; name and Enabled still change", async () => {
    viewer.current = { ...viewer.current, role: "admin" };
    mount({
      values: {
        ...engineView().values,
        headers: [{ name: "X-Client", value: "home" }],
        lifecycle_mode: "managed",
        lifecycle_start: "systemctl --user start garage-llm",
        lifecycle_stop: "systemctl --user stop garage-llm",
        lifecycle_ready_s: 180,
      },
      v2: v2({
        process: {
          driver: "command",
          start: ["systemctl", "--user", "start", "garage-llm"],
        },
      }),
    });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    for (const heading of [
      "Connection",
      "What it offers",
      "How it runs",
      "What Azizam can check",
      "Resources",
    ])
      expect(
        within(section(heading)).getAllByText(OWNER_ONLY, { exact: false })
          .length,
      ).toBeGreaterThan(0);
    expect(screen.getByLabelText("Address")).toBeDisabled();
    expect(
      screen.getByText("Only the owner can replace the key."),
    ).toBeVisible();
    for (const value of screen.getAllByLabelText("Value"))
      expect(value).toBeDisabled();
    expect(screen.getByLabelText("Time to wait for a reply")).toBeDisabled();
    const offers = section("What it offers");
    for (const box of within(offers).getAllByRole("checkbox"))
      expect(box).toBeDisabled();
    expect(
      within(offers).queryByRole("button", { name: "Find new models" }),
    ).toBeNull();
    expect(within(offers).queryByText("Add an adjustment")).toBeNull();
    const runs = section("How it runs");
    expect(
      within(runs).queryByLabelText("Who starts and stops this server"),
    ).toBeNull();
    expect(
      within(runs).getByText(
        /Only the owner can change this\. Azizam starts and stops this server with the commands below\./,
      ),
    ).toBeVisible();
    // The name saves with the plain edit, and nothing else is sent.
    const edit = vi.spyOn(api.services, "edit").mockResolvedValue(registry);
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Name"), "!");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() =>
      expect(edit).toHaveBeenCalledWith("garage-gpu", { name: "garage-gpu!" }),
    );
    expect(update).not.toHaveBeenCalled();
    await user.click(screen.getByRole("switch", { name: "Enabled" }));
    await waitFor(() => expect(edit).toHaveBeenCalledWith("garage-gpu", { enabled: false }));
    expect(update).not.toHaveBeenCalled();
    for (const [, body] of edit.mock.calls) {
      expect(body).not.toHaveProperty("definition");
      expect(body).not.toHaveProperty("values");
    }
  });

  it("schema mismatch: unsupported settings never render as controls; offerings show read-only; name and Enabled are metadata-only", async () => {
    const why = "This engine speaks another protocol than the openai-compatible settings describe.";
    mount({
      unsupported_fields: { address: why, api_key: why, path_prefix: why, transforms: why, models: why },
      can_edit_offerings: false,
      v2: v2({ connection: { ...v2().connection, address: "ws://10.0.0.7:9000", protocol: "other-protocol" } }),
    });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    const connection = section("Connection");
    expect(within(connection).queryByLabelText("Address")).toBeNull();
    expect(within(connection).getByTestId("address-read-only")).toHaveTextContent("ws://10.0.0.7:9000");
    expect(within(connection).getByTestId("unsupported-note")).toHaveTextContent(why);
    expect(within(connection).getByTestId("unsupported-note")).toHaveTextContent("Not shown: Address, API key, Extra path before every request.");
    expect(screen.queryByRole("button", { name: /Replace key|Add a key/ })).toBeNull();
    expect(screen.queryByLabelText("Extra path before every request")).toBeNull();
    // Supported settings still edit: this is per field, not a page lock.
    expect(screen.getByLabelText("Time to wait for a reply")).toBeEnabled();
    expect(screen.queryByText(OWNER_ONLY)).toBeNull();
    const offers = section("What it offers");
    expect(within(offers).getByTestId("unsupported-note")).toHaveTextContent(why);
    expect(within(offers).queryByRole("checkbox")).toBeNull();
    expect(within(offers).queryByLabelText("Name apps use")).toBeNull();
    expect(within(offers).queryByText("Add an adjustment")).toBeNull();
    expect(within(offers).getByTestId("offering-facts")).toHaveTextContent("qwen3-32b");
    const edit = vi.spyOn(api.services, "edit").mockResolvedValue(registry);
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Name"), "!");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(edit).toHaveBeenCalledWith("garage-gpu", { name: "garage-gpu!" }));
    await user.click(screen.getByRole("switch", { name: "Enabled" }));
    await waitFor(() => expect(edit).toHaveBeenCalledWith("garage-gpu", { enabled: false }));
    expect(update).not.toHaveBeenCalled();
  });

  it("schema mismatch: a supported setting saves without the unsupported ones", async () => {
    const why = "Not used by this protocol.";
    mount({ unsupported_fields: { address: why, transforms: why }, can_edit_offerings: false });
    const user = userEvent.setup();
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    await user.click(await screen.findByText("More options", { selector: "summary span" }));
    const timeout = screen.getByLabelText("Time to wait for a reply");
    await user.clear(timeout);
    await user.type(timeout, "300");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    const body = update.mock.calls[0][1];
    expect(body.values).toMatchObject({ reply_timeout_s: 300 });
    expect(body.values).not.toHaveProperty("address");
    expect(body.values).not.toHaveProperty("transforms");
    expect(body).not.toHaveProperty("models");
    expect(body).not.toHaveProperty("capabilities");
  });

  it("the owner's edits to every shown field reach the save body", async () => {
    mount();
    const user = userEvent.setup();
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    await user.click(
      await screen.findByText("More options", { selector: "summary span" }),
    );
    const timeout = screen.getByLabelText("Time to wait for a reply");
    await user.clear(timeout);
    await user.type(timeout, "300");
    await user.selectOptions(
      screen.getByLabelText("Who starts and stops this server"),
      "managed",
    );
    await user.type(screen.getByLabelText("Start command"), "run-it");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1].values).toMatchObject({
      reply_timeout_s: 300,
      lifecycle_mode: "managed",
      lifecycle_start: "run-it",
    });
  });
});

describe("a test traces exactly what it sent", () => {
  it("shows the method, URL, redacted headers, body and time limit", async () => {
    const user = userEvent.setup();
    mount();
    const result = {
      ...testResult("chat_pass"),
      via: "saved" as const,
      request: {
        method: "POST",
        url: "http://192.168.1.50:8080/v1/chat/completions",
        headers: {
          authorization: "Bearer (hidden)",
          "content-type": "application/json",
        },
        body: { model: "qwen3-32b-instruct-q4_k_m", max_tokens: 1 },
        timeout_s: 600,
      },
    };
    vi.spyOn(enginesApi, "test").mockResolvedValue(result);
    vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    await user.click(
      await screen.findByRole("button", { name: "Run a real test for Chat" }),
    );
    const trace = await screen.findByLabelText("Request sent, with secrets hidden");
    expect(trace).toHaveTextContent(
      "POST http://192.168.1.50:8080/v1/chat/completions",
    );
    expect(trace).toHaveTextContent("authorization: Bearer (hidden)");
    expect(trace).toHaveTextContent('"max_tokens": 1');
    expect(
      screen.getByText("Waits up to 600 seconds for the reply."),
    ).toBeInTheDocument();
  });
});

it("owned process: shows the process driver and preserves its argv as read-only", async () => {
  mount({ lifecycle_editable: false, request_path: { mode: "gateway_lifecycle", source: "service record", url: "http://127.0.0.1:8081/v1/chat/completions" },
    v2: v2({ process: { driver: "process", start: ["/opt/server", "--port", "8081"], port: { fixed: 8081 } },
      observations: { inventory: { http: { path: "/models" }, select: "/data/*/id" } } }) });
  await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
  const runs = section("How it runs");
  expect(within(runs).getByTestId("process-facts")).toHaveTextContent("Azizam. It starts the process itself");
  expect(runs).toHaveTextContent("/opt/server --port 8081");
  expect(within(runs).queryByRole("textbox")).toBeNull();
  expect(section("What Azizam can check")).toHaveTextContent("reads /data/*/id");
  expect(screen.getByLabelText("Address")).toBeEnabled();
});

it("administrator: a failed first-use check prevents enabling the saved connection", async () => {
  viewer.current.role = "administrator";
  const edit = vi.spyOn(api.services, "edit").mockResolvedValue(registry);
  const result = checkFixture("oai_down_admin");
  const check = vi.spyOn(enginesApi, "check").mockResolvedValue(result);
  mount({ enabled: false, check: { status: "not_started" } as never });
  await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
  await userEvent.setup().click(screen.getByRole("switch", { name: "Enabled" }));
  await waitFor(() => expect(check).toHaveBeenCalled());
  await waitFor(() => expect(screen.getAllByText(result.findings[0].what).length).toBeGreaterThan(0));
  expect(edit).not.toHaveBeenCalled();
});

describe("saved contract facts acceptance", () => {
  it("lists every saved request operation and separates it from draft edits", () => {
    const view = engineView({ request_path: { mode: "direct", source: "saved routes", url: "/chat",
      operations: [{ capability: "chat", method: "POST", url: "/chat" }, { capability: "embedding", method: "PUT", url: "/embed" }] } as never });
    renderWithClient(<ConnectionFacts view={view} definition={{ ...definitions.definitions[0], fields: [] }} />);
    expect(screen.getByTestId("connection-facts")).toHaveTextContent("Saved request destinations");
    expect(screen.getByTestId("connection-facts")).toHaveTextContent("embedding: PUT /embed");
    expect(screen.getByTestId("connection-facts")).toHaveTextContent("Draft edits do not change these destinations until saved.");
  });
  it("shows both credential destinations without an API key editor and keeps a missing destination unknown", () => {
    const record = v2();
    record.connection.credentials.inference = { secret_ref: "configured" };
    record.connection.credentials.control = { origin: "https://control.example", header: "X-Key" };
    const view = engineView({ v2: record, credential_destination: null, key: { state: "configured" } });
    renderWithClient(<ConnectionFacts view={view} definition={{ ...definitions.definitions[0], fields: [] }} />);
    const facts = screen.getByTestId("connection-facts");
    expect(facts).toHaveTextContent("Inference key is sent toThe saved key’s destination is unknown.");
    expect(facts).toHaveTextContent("Control key destinationhttps://control.example");
    expect(facts).not.toHaveTextContent("No inference key is configured.");
  });
  it("shows workflow tasks, inputs, options, profiles, adjustments and evidence safely", () => {
    renderWithClient(<OfferingFacts v2={v2({ offerings: [{ id: "workflow-long-id", kind: "workflow",
      tasks: [{ task: "image", evidence: "declared" }], required_inputs: [{ name: "prompt", type: "text" }],
      options: { size: ["small", "large"] }, start_profiles: [{ id: "quiet", settings: { threads: 2 } }],
      adjustments: [{ kind: "default-params", set: { seed: 4 } }],
      selection: { mode: "whole_graph" } }] })} />);
    const facts = screen.getByTestId("offering-facts");
    for (const word of ["image", "declared", "Required inputs", "prompt", "Options", "small", "Start settings", "quiet", "Adjustments", "seed"]) expect(facts).toHaveTextContent(word);
    expect(facts).not.toHaveTextContent("[object Object]");
    expect(screen.getByText("Required inputs").nextElementSibling).not.toHaveTextContent("{");
  });
  it("does not turn unknown selection or unknown process settings into verified claims", () => {
    renderWithClient(<OfferingFacts v2={v2({ offerings: [{ id: "a", kind: "model", selection: { mode: "switch" } }], selected_offering: "a" })} />);
    expect(screen.getByTestId("offering-facts")).toHaveTextContent("Not verified by a check.");
  });
  it("shows declared check methods, inventory meaning and source next to saved evidence", () => {
    const record = v2({ observations: { inventory: { http: { path: "/inventory", method: "POST" } as never, means: "loaded",
      last: { ok: true, at: 1700000000, detail: "one", source: "server report" } as never } } });
    renderWithClient(<ObservationList view={engineView({ v2: record })} />);
    const facts = screen.getByTestId("observations");
    expect(facts).toHaveTextContent("POST /inventory");
    expect(facts).toHaveTextContent("Models loaded in memory");
    expect(facts).toHaveTextContent("Source: server report");
    expect(facts).toHaveTextContent("Saved result: one");
  });
  it("explains an unknown check method without guessing and uses GET for existing HTTP checks", () => {
    renderWithClient(<ObservationList view={engineView({ v2: v2({ observations: { process_ready: { command: ["check"] }, inventory: { http: { path: "/models" }, means: "available" } } }) })} />);
    expect(screen.getByTestId("observations")).toHaveTextContent("A check is configured, but its method is not explained.");
    expect(screen.getByTestId("observations")).toHaveTextContent("GET /models");
    expect(screen.getByTestId("observations")).toHaveTextContent("Models available to use");
  });
  it("shows reload arguments and the saved file role without implying a move changes configuration", () => {
    renderWithClient(<ProcessFacts lifecycleEditable={false} view={engineView({ v2: v2({
      process: { driver: "command", profile_apply: { kind: "write_env_file_then_restart", file: "/settings.env", reload: ["reload", "two words"] } },
      residency: { idle_owner: "gateway" } }) })} />);
    const facts = screen.getByTestId("process-facts");
    expect(facts).toHaveTextContent('Reloadreload "two words"');
    expect(facts).toHaveTextContent("write_env_file_then_restart");
    expect(facts).toHaveTextContent("The saved process settings choose this path.");
    expect(facts).toHaveTextContent("Moving the file does not update those settings.");
    expect(facts).toHaveTextContent("The idle delay is unknown.");
    expect(facts).not.toHaveTextContent("? seconds");
  });
  it("keeps nested resources readable and qualifies every shape without claiming planning", () => {
    renderWithClient(<ResourceFacts v2={v2({ resources: { gpu: { devices: [{ id: 0 }], enforced: false }, memory: 12, custom: [1, { unit: "GB" }], cpu: { cores: 4, enforced: true } } })} />);
    const facts = screen.getByTestId("resource-facts");
    expect(facts).toHaveTextContent('"id":0');
    expect(facts).toHaveTextContent("Resource estimate only; not enforced.");
    expect(facts).toHaveTextContent("Enforcement unknown.");
    expect(facts).toHaveTextContent("Enforced.");
    expect(facts).not.toHaveTextContent("[object Object]");
    expect(facts).not.toHaveTextContent("plans");
  });
  it("shows only this section's unsupported reasons", () => {
    renderWithClient(<UnsupportedNote fields={[{ key: "address", label: "Address", type: "url" }]} unsupported={{ address: "Address cannot be edited.", other: "Other section only." }} />);
    expect(screen.getByTestId("unsupported-note")).toHaveTextContent("Address cannot be edited.");
    expect(screen.getByTestId("unsupported-note")).not.toHaveTextContent("Other section only.");
    expect(screen.queryByRole("link")).toBeNull();
  });
});
