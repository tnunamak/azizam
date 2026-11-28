import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { QueryClient } from "@tanstack/react-query";
import { AppShell } from "@/components/shell/app-shell";
import { resetSessionCache } from "@/lib/api";

// The shell health pill must say what its dot says (DESIGN.md, Status model
// rule 3): faults are "issues", capabilities with no fresh check are
// "Not checked yet", and the popover lists the capabilities behind the count.

const clients = vi.hoisted(() => [] as QueryClient[]);
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    createQueryClient: () => {
      const client = actual.createQueryClient();
      clients.push(client);
      return client;
    },
  };
});

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(""),
  useRouter: () => ({ push: vi.fn() }),
}));

const member = {
  authenticated: true,
  csrf_token: "csrf",
  user: {
    id: "m",
    name: "Mina Member",
    email: "m@example.test",
    role: "member",
  },
  apps: [],
};

function json(body: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
}

type Row = {
  key: string;
  title: string;
  status: string;
  reason?: string;
  fresh?: boolean;
  ageS?: number;
};

function capability(row: Row) {
  const now = Math.floor(Date.now() / 1000);
  return {
    capability: row.key,
    capability_title: row.title,
    icon_key: row.key,
    intent: `${row.key}.run`,
    model: "default",
    available: row.status === "ready",
    status: row.status,
    reason_code: row.reason ?? row.status,
    ...(row.fresh
      ? {
          availability: {
            evidence: {
              source: "health_check",
              observed_at_s: now - (row.ageS ?? 5),
            },
          },
        }
      : {}),
  };
}

function serve(rows: Row[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/me/api/session")) return json(member);
      if (url.endsWith("/health")) return json({ status: "healthy" });
      if (url.endsWith("/me/api/home"))
        return json({
          authenticated: true,
          user: member.user,
          aliases: [],
          receipts: [],
          capabilities: rows.map(capability),
        });
      return json({});
    }),
  );
}

beforeEach(() => {
  clients.forEach((client) => client.clear());
  resetSessionCache();
  window.history.replaceState(null, "", "/");
  localStorage.clear();
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  HTMLCanvasElement.prototype.getContext = vi.fn(() => null);
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ready = (key: string, title: string): Row => ({
  key,
  title,
  status: "ready",
  fresh: true,
});
const unknown = (key: string, title: string): Row => ({
  key,
  title,
  status: "unknown",
});
const down = (key: string, title: string): Row => ({
  key,
  title,
  status: "unavailable",
  reason: "unavailable",
});
const degraded = (key: string, title: string): Row => ({
  key,
  title,
  status: "degraded",
  fresh: true,
});

async function pill(name: string) {
  const buttons = await screen.findAllByRole("button", {
    name: `Gateway status: ${name}`,
  });
  return buttons[0];
}

describe("shell health pill", () => {
  it("reads 'Not checked yet' with a ring dot when the only problem is Unknown", async () => {
    serve([ready("chat", "Chat"), unknown("image", "Images")]);
    render(<AppShell>content</AppShell>);
    const button = await pill("Not checked yet");
    expect(screen.queryByRole("button", { name: /issue/ })).toBeNull();
    const dot = button.querySelector("span[aria-hidden]")!;
    expect(dot.className).toContain("border-neutral");
    expect(dot.className).not.toMatch(/bg-(danger|warning|success)-fill/);
  });

  it("does not put the unchecked count in the pill", async () => {
    serve([
      unknown("chat", "Chat"),
      unknown("image", "Images"),
      unknown("video", "Video"),
    ]);
    render(<AppShell>content</AppShell>);
    const user = userEvent.setup();
    await user.click(await pill("Not checked yet"));
    const popover = await screen.findByLabelText("Gateway health");
    expect(within(popover).getAllByRole("listitem")).toHaveLength(3);
  });

  it("keeps 'N issue' and the fault colour for a Down capability", async () => {
    serve([ready("chat", "Chat"), down("image", "Images")]);
    render(<AppShell>content</AppShell>);
    const button = await pill("1 issue");
    expect(button.querySelector("span[aria-hidden]")!.className).toContain(
      "bg-danger-fill",
    );
  });

  it("counts only faults as issues and takes the worst fault's colour in a mix", async () => {
    serve([
      unknown("chat", "Chat"),
      unknown("video", "Video"),
      degraded("music", "Music"),
      down("image", "Images"),
    ]);
    render(<AppShell>content</AppShell>);
    const button = await pill("2 issues");
    expect(button.querySelector("span[aria-hidden]")!.className).toContain(
      "bg-danger-fill",
    );
  });

  it("takes the amber colour when the worst fault is Degraded", async () => {
    serve([unknown("chat", "Chat"), degraded("music", "Music")]);
    render(<AppShell>content</AppShell>);
    const button = await pill("1 issue");
    expect(button.querySelector("span[aria-hidden]")!.className).toContain(
      "bg-warning-fill",
    );
  });

  it("lists the problem capabilities with their word and detail, and offers 'See why'", async () => {
    serve([
      ready("chat", "Chat"),
      { ...ready("image", "Images"), ageS: 7 * 3600 },
      down("video", "Video"),
    ]);
    render(<AppShell>content</AppShell>);
    const user = userEvent.setup();
    await user.click(await pill("1 issue"));
    const popover = await screen.findByLabelText("Gateway health");
    const list = within(popover).getByRole("list");
    const items = within(list).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      "Video · Down",
      "Images · UnknownLast checked 7 h ago.",
    ]);
    expect(
      within(popover).getByRole("link", { name: "See why" }),
    ).toHaveAttribute("href", "/status");
    expect(
      within(popover).queryByRole("link", { name: "View status" }),
    ).toBeNull();
  });

  it("keeps 'View status' and no list when nothing is wrong", async () => {
    serve([ready("chat", "Chat"), ready("image", "Images")]);
    render(<AppShell>content</AppShell>);
    const user = userEvent.setup();
    await user.click(await pill("All working"));
    const popover = await screen.findByLabelText("Gateway health");
    expect(within(popover).queryByRole("list")).toBeNull();
    expect(
      within(popover).getByRole("link", { name: "View status" }),
    ).toBeVisible();
    expect(within(popover).queryByRole("link", { name: "See why" })).toBeNull();
  });
});
