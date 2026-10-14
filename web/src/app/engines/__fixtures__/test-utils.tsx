/**
 * Shared test data for the Add engine and engine record specs. The JSON comes
 * from the real backend code (definitions, checks, tests, workflow reads), made
 * by running the check against fake servers, so the specs see the real shapes.
 */
import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type {
  CheckResult,
  EngineDefinitions,
  EngineView,
  TestResult,
  WorkflowRead,
} from "@/lib/engines-api";
import raw from "./engine-fixtures.json";

const data = raw as unknown as {
  definitions: EngineDefinitions;
  checks: Record<string, CheckResult>;
  tests: Record<string, TestResult>;
  workflows: { ok: WorkflowRead; bad: WorkflowRead };
  workflow_text: string;
};

export const definitions = data.definitions;
export const workflowText = data.workflow_text;

/** A deep copy, so a spec can change it freely. */
export function check(name: string): CheckResult {
  return structuredClone(data.checks[name]);
}
export function testResult(name: string): TestResult {
  return structuredClone(data.tests[name]);
}
export function workflowRead(name: "ok" | "bad"): WorkflowRead {
  return structuredClone(data.workflows[name]);
}
export function definition(id: string) {
  const found = definitions.definitions.find((d) => d.definition === id);
  if (!found) throw new Error(`no definition ${id}`);
  return found;
}

export function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  return { client, ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) };
}

/** A stored OpenAI-compatible engine, as GET /admin/api/engines/{id} returns it. */
export function engineView(over: Partial<EngineView> = {}): EngineView {
  return {
    service_id: "garage-gpu",
    definition: "openai-compatible",
    definition_version: 1,
    inferred: false,
    name: "garage-gpu",
    enabled: true,
    locked: false,
    lifecycle_editable: true,
    values: {
      address: "http://192.168.1.50:8080/v1",
      path_prefix: "",
      auth_header: "Authorization",
      connect_timeout_s: 5,
      reply_timeout_s: 120,
      headers: [],
      transforms: ["reasoning-effort-final-chat"],
      lifecycle_mode: "external",
    },
    key: { state: "configured", source: "stored", origin: "http://192.168.1.50:8080" },
    capabilities: ["chat", "embeddings"],
    models: [
      { id: "qwen3-32b-instruct-q4_k_m", public_name: "qwen3-32b", context_window: 40960, enabled: true },
      { id: "nomic-embed-text-v1.5", public_name: "nomic-embed-text", context_window: 8192, enabled: true },
    ],
    tests: {},
    check: { status: "ok", checked_at: Math.floor(Date.now() / 1000) - 120, key_sent: true, model_count: 2 },
    workflow: null,
    calls: "GET http://192.168.1.50:8080/v1/models",
    other_settings: { probe_interval_s: 30, legacy_note: "bought 2025" },
    record: {
      id: "garage-gpu",
      name: "garage-gpu",
      enabled: true,
      config: { address: "http://192.168.1.50:8080/v1", api_key_secret_id: "configured", probe_interval_s: 30 },
    },
    ...over,
  };
}
