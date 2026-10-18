import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { enginesApi, type EngineView } from "@/lib/engines-api";
import { accessAnswer, definitions, engineView, renderWithClient } from "../__fixtures__/test-utils";
import { EngineRecordPage } from "./engine-record-page";

const viewer = vi.hoisted(() => ({
  current: { authenticated: true, name: "Tim", email: null, role: "owner", admin: true },
}));
vi.mock("@/components/shell/session", () => ({ useViewer: () => viewer.current }));

const registry = { schema_version: 1, services: {} } as never;
let view: EngineView;

function mount(over: Partial<EngineView> = {}) {
  view = engineView(over);
  vi.spyOn(enginesApi, "view").mockImplementation(async () => structuredClone(view));
  return renderWithClient(
    <EngineRecordPage serviceId={view.service_id} onBack={() => undefined} onRemove={() => undefined} />,
  );
}

function requestPath(url: string): EngineView["request_path"] {
  return { mode: "direct", source: "service record", url } as EngineView["request_path"];
}

beforeEach(() => {
  viewer.current = { authenticated: true, name: "Tim", email: null, role: "owner", admin: true };
  vi.spyOn(enginesApi, "definitions").mockResolvedValue(structuredClone(definitions));
  vi.spyOn(enginesApi, "access").mockResolvedValue(accessAnswer("nobody"));
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("the reply timeout on Connection", () => {
  it("shows the effective seconds and where the value comes from", async () => {
    mount({
      values: { ...engineView().values, reply_timeout_s: 1800 },
      reply_timeout: { seconds: 1800, source: "default for engines Azizam starts" },
    });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    expect(screen.getByTestId("connection-facts")).toHaveTextContent(
      "Time to wait for a reply1800 seconds. Source: default for engines Azizam starts.",
    );
  });

  it("names a saved value as saved", async () => {
    mount({ reply_timeout: { seconds: 300, source: "saved on this engine" } });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    expect(screen.getByTestId("connection-facts")).toHaveTextContent(
      "Time to wait for a reply300 seconds. Source: saved on this engine.",
    );
  });
});

describe("a direct record with no saved timeout", () => {
  it("shows the source alone, invents no number, and saves the empty value unchanged", async () => {
    const user = userEvent.setup();
    mount({
      values: { ...engineView().values, reply_timeout_s: "" },
      reply_timeout: { seconds: null, source: "model router default (no saved timeout)" },
    });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    const facts = screen.getByTestId("connection-facts");
    expect(facts).toHaveTextContent("Time to wait for a replymodel router default (no saved timeout).");
    expect(facts).not.toHaveTextContent(/null|NaN|\d+ seconds/);
    expect(screen.getByLabelText("Time to wait for a reply")).toHaveValue("");
    await user.click(screen.getByText("More options", { selector: "summary span" }));
    await user.click(screen.getByRole("button", { name: "Add a header" }));
    await user.type(screen.getByLabelText("Header name"), "X-Region");
    await user.type(screen.getByLabelText("Value for X-Region"), "lan");
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    const commands = vi.spyOn(enginesApi, "secretEntries").mockResolvedValue(registry);
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(commands).toHaveBeenCalled());
    expect(commands.mock.calls[0][1]).toEqual([{ carrier: "config.headers", operation: "add", name: "X-Region", value: "lan" }]);
    expect(update).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Time to wait for a reply")).toHaveValue("");
  });
});

describe("attention from the gateway", () => {
  it("shows a missing key variable where the owner can see it", async () => {
    mount({
      attention: [{ code: "engine_credential_unavailable", message: "Key not available: AZ_KEY is not set" }],
    });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    expect(screen.getByRole("alert")).toHaveTextContent("Key not available: AZ_KEY is not set");
  });
});

describe("configured header entries", () => {
  const headers = [{ id: "ent_auth", name: "Authorization", configured: true }, { id: "ent_client", name: "X-Client", configured: true }];
  const record = { ...engineView().record, config: { headers } };

  it("shows configured metadata with empty inputs and omits untouched entries from an unrelated save", async () => {
    const user = userEvent.setup();
    mount({ record });
    const group = await screen.findByRole("group", { name: "Request headers" });
    for (const header of headers) {
      const input = within(group).getByLabelText(`Value for ${header.name}`);
      expect(input).toHaveValue("");
      expect(input).toHaveAttribute("type", "password");
    }
    expect(within(group).getAllByText("Configured")).toHaveLength(2);
    expect(screen.queryByDisplayValue("[redacted]")).toBeNull();
    await user.clear(screen.getByLabelText("Time to wait for a reply"));
    await user.type(screen.getByLabelText("Time to wait for a reply"), "300");
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    const commands = vi.spyOn(enginesApi, "secretEntries").mockResolvedValue(registry);
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1].values).not.toHaveProperty("headers");
    expect(commands).not.toHaveBeenCalled();
  });

  it("removes a configured header with an explicit clear keyed by its stable id", async () => {
    const user = userEvent.setup();
    mount({ record });
    await user.click(await screen.findByRole("button", { name: "Remove header Authorization" }));
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    const commands = vi.spyOn(enginesApi, "secretEntries").mockResolvedValue(registry);
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(commands).toHaveBeenCalled());
    expect(commands.mock.calls[0][1]).toEqual([{ carrier: "config.headers", operation: "clear", entry_id: "ent_auth" }]);
    expect(update).not.toHaveBeenCalled();
    expect(JSON.stringify(commands.mock.calls)).not.toContain("[redacted]");
  });
});

describe("Inference key is sent to", () => {
  it("names the origin when the request goes to the same origin", async () => {
    mount({ request_path: requestPath("http://192.168.1.50:8080/v1/chat/completions") });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    expect(screen.getByTestId("connection-facts")).toHaveTextContent("Inference key destinationConfigured for http://192.168.1.50:8080. Availability is checked when the key is used.");
  });

  it("says no key is sent when the engine address is another origin", async () => {
    mount({
      request_path: requestPath("http://10.0.0.9:8080/v1/chat/completions"),
      key: { state: "configured", source: "stored", origin: "http://192.168.1.50:8080" },
    });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    const facts = screen.getByTestId("connection-facts");
    expect(facts).toHaveTextContent(
      "No inference key is sent: the key belongs to http://192.168.1.50:8080 but this engine’s address is http://10.0.0.9:8080",
    );
    expect(facts).not.toHaveTextContent("only. Azizam never sends it");
  });

  it("treats an explicit default port as the same origin", async () => {
    mount({
      request_path: requestPath("https://gpu.example/v1/chat/completions"),
      key: { state: "configured", source: "stored", origin: "https://gpu.example:443" },
    });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    expect(screen.getByTestId("connection-facts")).not.toHaveTextContent("No key is sent");
    cleanup();
    mount({
      request_path: requestPath("http://gpu.example:80/v1/chat/completions"),
      key: { state: "configured", source: "stored", origin: "http://gpu.example" },
    });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    expect(screen.getByTestId("connection-facts")).not.toHaveTextContent("No key is sent");
  });

  it("treats a different port as a different origin", async () => {
    mount({
      request_path: requestPath("http://gpu.example:8081/v1/chat/completions"),
      key: { state: "configured", source: "stored", origin: "http://gpu.example:8080" },
    });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    expect(screen.getByTestId("connection-facts")).toHaveTextContent("No inference key is sent: the key belongs to");
  });
});

it("does not claim a key destination when the request URL is unknown", async () => {
  mount({ request_path: requestPath(""), key: { state: "configured", source: "stored", origin: "https://gpu.example" } });
  await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
  expect(screen.getByTestId("connection-facts")).not.toHaveTextContent("only. Azizam never sends it");
  expect(screen.getByTestId("connection-facts")).toHaveTextContent("Unknown. The request address is unavailable.");
});

it("says no key is sent when the configured environment key is unavailable", async () => {
  mount({ request_path: requestPath("https://gpu.example/v1/chat/completions"),
    key: { state: "configured", source: "env", env_name: "MISSING_KEY", origin: "https://gpu.example" },
    attention: [{ code: "engine_credential_unavailable", message: "Key not available: MISSING_KEY is not set" }] });
  await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
  expect(screen.getByTestId("connection-facts")).toHaveTextContent("No inference key is sent. The configured key is not available.");
});
