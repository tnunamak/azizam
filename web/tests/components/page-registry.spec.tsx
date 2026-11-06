import { existsSync, readdirSync } from "node:fs";
import path from "node:path";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { QueryClient } from "@tanstack/react-query";
import { AppShell } from "@/components/shell/app-shell";
import { resetSessionCache } from "@/lib/api";
import { destinations, uiPages } from "@/lib/product";
import registry from "../../../gateway/ui_pages.json";

// gateway/ui_pages.json is the one list of pages (CONNECT_ROOT_MAIL D2.2).
// The gateway serves and guards from it; these tests hold the web app to it.

const appDir = path.join(process.cwd(), "src", "app");

function pageDirectories(dir: string, prefix = ""): string[] {
  const found = existsSync(path.join(dir, "page.tsx")) ? [prefix] : [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    found.push(
      ...pageDirectories(
        path.join(dir, entry.name),
        prefix ? `${prefix}/${entry.name}` : entry.name,
      ),
    );
  }
  return found;
}

const adminPages = Object.entries(registry.pages)
  .filter(([, access]) => access === "admin")
  .map(([name]) => name);
const memberPages = Object.entries(registry.pages)
  .filter(([, access]) => access === "member")
  .map(([name]) => name);

describe("page registry", () => {
  it("names exactly the directories that hold a page", () => {
    expect(pageDirectories(appDir).sort()).toEqual(
      Object.keys(registry.pages).sort(),
    );
  });

  it("is what the app reads", () => {
    expect(uiPages).toEqual(registry.pages);
    expect(adminPages).toContain("categories");
  });

  it("puts every admin page, and Status, in the Gateway group", () => {
    const gateway = destinations
      .filter((d) => d.group === "Gateway")
      .map((d) => d.href.slice(1))
      .sort();
    expect(gateway).toEqual([...adminPages, "status"].sort());
  });
});

// The shell keeps one query client per module; clear it between renders so
// each test sees its own session.
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

const push = vi.fn();
const replace = vi.fn();
let pathname = "/";

vi.mock("next/navigation", () => ({
  usePathname: () => pathname,
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push, replace }),
}));

function session(role: "owner" | "member") {
  return {
    authenticated: true,
    csrf_token: "csrf",
    user: {
      id: role,
      name: role === "owner" ? "Alex Owner" : "Sam Member",
      email: `${role}@example.test`,
      role,
    },
    apps: [],
  };
}

function stubGateway(role: "owner" | "member") {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.endsWith("/me/api/session")
        ? session(role)
        : url.endsWith("/health")
          ? { status: "healthy" }
          : {};
      return Promise.resolve(Response.json(body));
    }),
  );
}

beforeEach(() => {
  clients.forEach((client) => client.clear());
  resetSessionCache();
  pathname = "/";
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
    class ResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("shell follows the registry", () => {
  it("shows an owner the Gateway group from the registry, Categories included", async () => {
    stubGateway("owner");
    pathname = "/library";
    window.history.replaceState(null, "", "/library/");
    render(
      <AppShell>
        <h1>Library consumer</h1>
      </AppShell>,
    );
    const nav = await screen.findByRole("navigation", {
      name: "Main navigation",
    });
    const label = within(nav).getByText("Gateway", { exact: true });
    const group = label.closest(".shell-group-admin") as HTMLElement;
    const hrefs = within(group)
      .getAllByRole("link")
      .map((link) => link.getAttribute("href")!.replace(/^\/|\/$/g, ""))
      .sort();
    expect(hrefs).toEqual([...adminPages, "status"].sort());
    expect(
      within(group).getByRole("link", { name: "Categories" }),
    ).toBeVisible();
  });

  it.each(adminPages)(
    "gates the admin page /%s/ for a member",
    async (name) => {
      stubGateway("member");
      pathname = `/${name}/`;
      window.history.replaceState(null, "", `/${name}/`);
      render(
        <AppShell>
          <h1>Admin consumer</h1>
        </AppShell>,
      );
      expect(
        await screen.findByText("Owner access required"),
      ).toBeInTheDocument();
      expect(screen.queryByText("Admin consumer")).toBeNull();
    },
  );

  it.each(memberPages)("opens the member page /%s/ for a member", async (name) => {
    stubGateway("member");
    pathname = `/${name}/`;
    window.history.replaceState(null, "", `/${name}/`);
    render(
      <AppShell>
        <h1>Member consumer</h1>
      </AppShell>,
    );
    expect(
      await screen.findByRole("heading", { name: "Member consumer" }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText("Owner access required")).toBeNull(),
    );
  });
});

describe("public pages for a signed-out visitor", () => {
  function stubSignedOut() {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/admin/api/session"))
          return Promise.resolve(
            Response.json(
              { error: { code: "authentication_required", message: "Sign in" } },
              { status: 401 },
            ),
          );
        return Promise.resolve(
          Response.json(url.endsWith("/me/api/session") ? { authenticated: false } : {}),
        );
      }),
    );
  }

  it.each([
    ["Home", "/"],
    ["an old /ui URL (the 404 page)", "/ui/home/"],
    ["Sign in", "/sign-in/"],
  ])("renders %s without a sign-in redirect", async (_label, path) => {
    stubSignedOut();
    pathname = path;
    window.history.replaceState(null, "", path);
    render(
      <AppShell>
        <h1>Public consumer</h1>
      </AppShell>,
    );
    expect(
      await screen.findByRole("heading", { name: "Public consumer" }),
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Checking your session…")).toBeNull());
    expect(replace).not.toHaveBeenCalled();
  });

  it("sends a signed-out visitor on a member page to sign in with its path", async () => {
    stubSignedOut();
    pathname = "/library/";
    window.history.replaceState(null, "", "/library/?tab=mine");
    render(
      <AppShell>
        <h1>Private consumer</h1>
      </AppShell>,
    );
    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith(
        "/sign-in/?return_to=%2Flibrary%2F%3Ftab%3Dmine",
      ),
    );
    expect(screen.queryByText("Private consumer")).toBeNull();
  });
});
