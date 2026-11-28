import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, resetSessionCache, type OverviewPayload } from "@/lib/api";
import { StatusClient } from "./status-client";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/provider-status", () => ({
  useOptionalProviderStatus: () => ({
    state: "hidden",
    providers: [],
    refetch: vi.fn(),
  }),
}));
vi.mock("@/lib/use-capability-views", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/use-capability-views")>()),
  useActivitySample: () => ({
    data: [],
    isPending: false,
    isError: false,
    refetch: vi.fn(),
  }),
}));
vi.mock("@/app/_home/home-api", () => ({
  homeApi: {
    home: async () => ({
      authenticated: true,
      capabilities: [],
      aliases: [],
      receipts: [],
      apps: [],
    }),
  },
}));
vi.mock("next/link", () => ({
  default: ({ children, href }: React.ComponentProps<"a">) => (
    <a href={href}>{children}</a>
  ),
}));

const healthy: OverviewPayload = {
  attention: [],
  registry_apply: null,
  registry_excluded: [],
};
const partial: OverviewPayload = {
  attention: [
    {
      kind: "registry",
      severity: "problem",
      title: "Some saved engines are not running",
      detail: "The gateway is serving 2 of 3 saved engines.",
    },
  ],
  registry_apply: {
    mode: "partial",
    error: "validation failed",
    left_out: { broken: "base_url is not a URL" },
    served: 2,
  },
  registry_excluded: [
    { section: "services", id: "broken", error: "base_url is not a URL" },
  ],
};

// The overview and the reload go through the real transport, so the spec
// sees the method, path and CSRF headers the browser would send.
let overview: OverviewPayload | "fail";
let reload: () => Response;
let fetchMock: ReturnType<typeof vi.fn>;
const calls = (path: string) =>
  fetchMock.mock.calls.filter(([url]) => url === path);

function mount() {
  vi.spyOn(api.services, "registry").mockResolvedValue({
    schema_version: 1,
    services: {},
  });
  vi.spyOn(api, "status").mockResolvedValue({
    current_health: {
      state: "ok",
      tone: "ok",
      label: "Ready",
      detail: "Ready",
      observation_available: true,
      services: [],
    },
  });
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })}
    >
      <StatusClient />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  resetSessionCache();
  overview = healthy;
  reload = () => Response.json({ reloaded: true });
  fetchMock = vi.fn(async (url: string) => {
    if (url === "/admin/api/session")
      return Response.json({
        actor: { subject: "owner", email: null, roles: ["owner"], csrf_token: "csrf-1" },
      });
    if (url === "/admin/api/overview")
      return overview === "fail"
        ? Response.json({ error: { code: "overview_failed", message: "no" } }, { status: 500 })
        : Response.json(overview);
    if (url === "/admin/api/config/reload") return reload();
    return new Response(null, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  resetSessionCache();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("names the registry problem and each left-out record", async () => {
  overview = partial;
  mount();
  expect(
    await screen.findByRole("heading", { name: "Some saved engines are not running" }),
  ).toBeVisible();
  expect(screen.getByText("The gateway is serving 2 of 3 saved engines.")).toBeVisible();
  expect(screen.getByText("broken: base_url is not a URL")).toBeVisible();
  expect(screen.getByRole("button", { name: "Apply again" })).toBeEnabled();
});

it("applies again once with the write headers and hides the problem once it is gone", async () => {
  overview = partial;
  // The stored registry applies cleanly this time, so the overview reads healthy.
  reload = () => {
    overview = healthy;
    return Response.json({ reloaded: true });
  };
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: "Apply again" }));
  await waitFor(() =>
    expect(
      screen.queryByRole("heading", { name: "Some saved engines are not running" }),
    ).toBeNull(),
  );
  expect(screen.queryByText("broken: base_url is not a URL")).toBeNull();
  expect(screen.queryByRole("button", { name: "Apply again" })).toBeNull();
  const [[, init]] = calls("/admin/api/config/reload");
  expect(calls("/admin/api/config/reload")).toHaveLength(1);
  expect(init.method).toBe("POST");
  expect(new Headers(init.headers).get("x-azizam-csrf")).toBe("csrf-1");
  expect(new Headers(init.headers).get("x-azizam-admin-action")).toBe("1");
});

it("disables the button and says Applying while the reload runs", async () => {
  overview = partial;
  let finish: (response: Response) => void = () => undefined;
  const pending = new Promise<Response>((resolve) => (finish = resolve));
  const base = fetchMock.getMockImplementation()!;
  fetchMock.mockImplementation((url: string) =>
    url === "/admin/api/config/reload" ? pending : base(url),
  );
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: "Apply again" }));
  expect(await screen.findByRole("button", { name: "Applying…" })).toBeDisabled();
  finish(Response.json({ reloaded: true }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Apply again" })).toBeEnabled(),
  );
});

it("shows a plain message when the reload fails, without the raw response", async () => {
  overview = partial;
  reload = () =>
    Response.json(
      { error: { code: "reload_failed", message: "Traceback opk_secret123 at line 4" } },
      { status: 500 },
    );
  const user = userEvent.setup();
  mount();
  await user.click(await screen.findByRole("button", { name: "Apply again" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "The gateway could not complete this request.",
  );
  expect(screen.queryByText(/Traceback|opk_secret123/)).toBeNull();
  // The problem stays, and the owner can try again.
  expect(screen.getByRole("button", { name: "Apply again" })).toBeEnabled();
  expect(
    screen.getByRole("heading", { name: "Some saved engines are not running" }),
  ).toBeVisible();
});

it("lists stored records that were left out even without an attention item", async () => {
  overview = { ...partial, attention: [] };
  mount();
  expect(
    await screen.findByRole("heading", { name: "Some saved records are not in use" }),
  ).toBeVisible();
  expect(screen.getByText("broken: base_url is not a URL")).toBeVisible();
});

it("shows no registry callout when the overview has no registry item", async () => {
  mount();
  await waitFor(() => expect(calls("/admin/api/overview")).toHaveLength(1));
  await screen.findByRole("table", { name: "Engine status" }).catch(() => undefined);
  expect(screen.queryByRole("button", { name: "Apply again" })).toBeNull();
  expect(screen.queryByText(/saved engines|saved records/)).toBeNull();
});

it("shows nothing extra when the overview request fails", async () => {
  overview = "fail";
  mount();
  await waitFor(() => expect(calls("/admin/api/overview").length).toBeGreaterThan(0));
  expect(screen.queryByRole("button", { name: "Apply again" })).toBeNull();
  expect(screen.getByRole("heading", { name: "Status" })).toBeVisible();
});
