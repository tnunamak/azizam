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

describe("attention from the gateway", () => {
  it("shows a missing key variable where the owner can see it", async () => {
    mount({
      attention: [{ code: "engine_credential_unavailable", message: "Key not available: AZ_KEY is not set" }],
    });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    expect(screen.getByRole("alert")).toHaveTextContent("Key not available: AZ_KEY is not set");
  });
});

describe("a header the gateway does not send", () => {
  const blocked = { name: "Authorization", value: "[redacted]", secret: true, not_sent: true };
  const plain = { name: "X-Client", value: "azizam-home" };

  it("says Not sent, cannot be edited, and keeps its metadata on save", async () => {
    const user = userEvent.setup();
    mount({ values: { ...engineView().values, headers: [blocked, plain] } });
    await screen.findByText("More options", { selector: "summary span" });
    const group = screen.getByRole("group", { name: "Request headers" });
    expect(within(group).getByText("Not sent")).toBeVisible();
    expect(screen.getByDisplayValue("Authorization")).toBeDisabled();
    expect(screen.getByDisplayValue("[redacted]")).toBeDisabled();
    await user.type(await screen.findByLabelText("Name"), "!");
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1].values?.headers).toEqual([blocked, plain]);
  });

  it("can be removed", async () => {
    const user = userEvent.setup();
    mount({ values: { ...engineView().values, headers: [blocked, plain] } });
    await user.click(await screen.findByRole("button", { name: "Remove header Authorization" }));
    const update = vi.spyOn(enginesApi, "update").mockResolvedValue(registry);
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1].values?.headers).toEqual([plain]);
  });
});

describe("Key is sent to", () => {
  it("names the origin when the request goes to the same origin", async () => {
    mount({ request_path: requestPath("http://192.168.1.50:8080/v1/chat/completions") });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    expect(screen.getByTestId("connection-facts")).toHaveTextContent("Key is sent tohttp://192.168.1.50:8080 only.");
  });

  it("says no key is sent when the engine address is another origin", async () => {
    mount({
      request_path: requestPath("http://10.0.0.9:8080/v1/chat/completions"),
      key: { state: "configured", source: "stored", origin: "http://192.168.1.50:8080" },
    });
    await screen.findByRole("heading", { level: 1, name: "garage-gpu" });
    const facts = screen.getByTestId("connection-facts");
    expect(facts).toHaveTextContent(
      "No key is sent: the key belongs to http://192.168.1.50:8080 but this engine’s address is http://10.0.0.9:8080",
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
    expect(screen.getByTestId("connection-facts")).toHaveTextContent("No key is sent: the key belongs to");
  });
});
