import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ role: "owner", success: vi.fn() }));
vi.mock("@/components/shell/session", () => ({
  useViewer: () => ({ role: mocks.role }),
}));
vi.mock("@/lib/toast", () => ({
  toast: { success: mocks.success, error: vi.fn() },
}));

import { resetSessionCache } from "@/lib/api";
import { SessionsSection } from "./sessions";

const payload = (idle = 30, absolute = 90, stepUp = 15, revision = "3") => ({
  sessions: { idle_timeout_days: idle, absolute_lifetime_days: absolute, step_up_minutes: stepUp },
  defaults: { idle_timeout_days: 30, absolute_lifetime_days: 90, step_up_minutes: 15 },
  bounds: { idle_timeout_days: [1, 90], absolute_lifetime_days: [1, 365], step_up_minutes: [1, 1440] },
  revision,
  source: "admin.db",
  apply_mode: "live",
});

type Sent = { method: string; path: string; body: unknown; ifMatch: string | null };
let sent: Sent[];
let put: (body: unknown) => Response;

beforeEach(() => {
  mocks.role = "owner";
  sent = [];
  put = () => Response.json(payload());
  resetSessionCache();
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    if (path === "/admin/api/session")
      return Response.json({ actor: { csrf_token: "csrf", roles: ["owner"], subject: "o", email: null } });
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    sent.push({ method, path, body, ifMatch: new Headers(init?.headers).get("if-match") });
    return method === "PUT" ? put(body) : Response.json(payload());
  });
});
afterEach(() => {
  cleanup();
  resetSessionCache();
});

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SessionsSection />
    </QueryClientProvider>,
  );
}
const idle = () => screen.findByLabelText("Sign out after this many days without use");
const absolute = () => screen.getByLabelText("Always sign in again after this many days");
const stepUp = () =>
  screen.getByLabelText("Ask for the password again for risky actions after this many minutes");
const save = () => screen.getByRole("button", { name: "Save sign-in session settings" });

describe("sign-in sessions card", () => {
  it("shows the saved numbers with their limits", async () => {
    mount();
    expect(await idle()).toHaveValue(30);
    expect(absolute()).toHaveValue(90);
    expect(stepUp()).toHaveValue(15);
    expect(screen.getByText("Whole days, from 1 to 90. Usually 30.")).toBeVisible();
    expect(screen.getByText("Whole days, from 1 to 365. Usually 90.")).toBeVisible();
    expect(screen.getByText("Whole minutes, from 1 to 1,440. Usually 15.")).toBeVisible();
    expect(save()).toBeDisabled();
  });

  it("saves exactly the three numbers with the revision in If-Match", async () => {
    put = () => Response.json(payload(45, 90, 15, "4"));
    const user = userEvent.setup();
    mount();
    const field = await idle();
    await user.clear(field);
    await user.type(field, "45");
    await user.click(save());
    await waitFor(() => expect(mocks.success).toHaveBeenCalledWith("Sign-in session settings saved"));
    const request = sent.find((call) => call.method === "PUT")!;
    expect(request).toMatchObject({
      path: "/admin/api/settings/sessions",
      ifMatch: "3",
      body: { idle_timeout_days: 45, absolute_lifetime_days: 90, step_up_minutes: 15 },
    });
    expect(Object.keys(request.body as object).sort()).toEqual([
      "absolute_lifetime_days",
      "idle_timeout_days",
      "step_up_minutes",
    ]);
    await waitFor(() => expect(save()).toBeDisabled());
    expect(field).toHaveValue(45);
  });

  it("shows the gateway's message for a refused number and keeps what was typed", async () => {
    put = () =>
      Response.json(
        {
          error: {
            code: "invalid_sessions",
            message: "idle_timeout_days must be a whole number from 1 to 90.",
          },
        },
        { status: 400 },
      );
    const user = userEvent.setup();
    mount();
    const field = await idle();
    await user.clear(field);
    await user.type(field, "120");
    await user.click(save());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unsaved changes. idle_timeout_days must be a whole number from 1 to 90.",
    );
    expect(field).toHaveValue(120);
    expect(mocks.success).not.toHaveBeenCalled();
  });

  it("names a conflict the way the other settings do", async () => {
    put = () =>
      Response.json({ error: { code: "settings_conflict", message: "Changed elsewhere." } }, { status: 409 });
    const user = userEvent.setup();
    mount();
    const field = await idle();
    await user.clear(field);
    await user.type(field, "20");
    await user.click(save());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Settings changed elsewhere; reload to see the latest",
    );
  });

  it("stops a number that is not whole before it reaches the gateway", async () => {
    const user = userEvent.setup();
    mount();
    const field = await idle();
    await user.clear(field);
    await user.type(field, "1.5");
    await user.click(save());
    expect(await screen.findByText("Enter a whole number.")).toBeVisible();
    expect(sent.some((call) => call.method === "PUT")).toBe(false);
  });

  it("leaves the card to the owner", async () => {
    mocks.role = "admin";
    mount();
    expect(await screen.findByText("Only the owner can change this.")).toBeVisible();
    expect(sent).toEqual([]);
    expect(screen.queryByRole("button", { name: /Save/ })).not.toBeInTheDocument();
  });
});
