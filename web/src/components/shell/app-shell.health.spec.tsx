import { readFileSync } from "node:fs";
import { parse } from "postcss";
import { CodeBlock } from "@/components/product/code-block";
import { CopyField } from "@/components/product/copy-field";
import { EntityName } from "@/components/product/entity-name";
import { FilterToggle } from "@/components/product/filter-toggle";
import { PageHeader } from "@/components/product/page-header";
import { Segmented } from "@/components/product/segmented";
import { SettingRow } from "@/components/product/setting-row";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "./app-shell";
import { resetSessionCache } from "@/lib/api";

// The shell health pill must say what its dot says (DESIGN.md, Status model
// rule 3), and the owner alert banner must name only a proven engine outage.
// Moved down from the health-pill, health-explanation and owner-alert browser
// specs; the browser keeps the real Status navigation and focus hand-off.

const clients = vi.hoisted(() => [] as QueryClient[]);
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    createQueryClient: () => {
      const client = actual.createQueryClient();
      // Production retries a 5xx twice with backoff, which no test waits for.
      // Set once here, keeping the other defaults, so no test depends on which
      // earlier test switched retries off.
      const defaults = client.getDefaultOptions();
      client.setDefaultOptions({ ...defaults, queries: { ...defaults.queries, retry: false } });
      clients.push(client);
      return client;
    },
  };
});

// The owner checks wait SHELL_CHECKS_DELAY_MS after mount (use-after-first-load.ts); these
// specs are about what the checks say, so they start at once. app-shell.deferred-checks.spec.tsx
// covers the delay itself.
vi.mock("./use-after-first-load", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./use-after-first-load")>()),
  useAfterFirstLoad: () => true,
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/models",
  useSearchParams: () => new URLSearchParams(""),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const member = {
  id: "member-pill",
  name: "Member",
  role: "member",
  email: "member@example.test",
};
const owner = { ...member, id: "owner", name: "Alex Owner", role: "owner" };

function json(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
}

type Row = ReturnType<typeof capability>;
function capability(
  key: string,
  title: string,
  state: "ready" | "unknown" | "down",
) {
  return {
    capability: key,
    capability_title: title,
    icon_key: key,
    intent: `${key}.run`,
    model: "default",
    available: state === "ready",
    status:
      state === "down" ? "unavailable" : state === "unknown" ? "unknown" : "ready",
    reason_code: state === "down" ? "unavailable" : state,
    ...(state === "ready"
      ? {
          availability: {
            evidence: {
              source: "health_check",
              observed_at_s: Math.floor(Date.now() / 1000),
            },
          },
        }
      : {}),
  };
}

interface World {
  user: typeof member;
  rows: Row[];
  health?: () => Promise<Response>;
  status?: unknown;
  providers?: unknown;
}
const requested: string[] = [];

function install(world: World) {
  requested.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      requested.push(new URL(url, "http://x.test").pathname);
      if (url.endsWith("/me/api/session"))
        return json({
          authenticated: true,
          csrf_token: "fixture",
          apps: [],
          user: world.user,
        });
      if (url.endsWith("/me/api/home"))
        return json({
          authenticated: true,
          user: world.user,
          aliases: [],
          receipts: [],
          capabilities: world.rows,
        });
      if (url.endsWith("/health"))
        return world.health ? world.health() : json({ status: "healthy" });
      if (url.includes("/admin/api/status")) return json({ status: world.status ?? {} });
      if (url.includes("/admin/api/providers"))
        return json(world.providers ?? { providers: [] });
      if (url.endsWith("/admin/api/operator/service-registry"))
        return json({ service_registry: { schema_version: 1, services: {} } });
      return json({});
    }),
  );
}

beforeEach(() => {
  clients.forEach((client) => client.clear());
  resetSessionCache();
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

async function openPill(name: string | RegExp) {
  render(
    <AppShell>
      <h1>Models</h1>
    </AppShell>,
  );
  const pill = (await screen.findAllByRole("button", { name }))[0];
  await userEvent.setup().click(pill);
  return { pill, popover: await screen.findByLabelText("Gateway health") };
}

const dotOf = (pill: HTMLElement) => pill.querySelector("span[aria-hidden]")!;

describe("shell health pill", () => {
  it("reads one Unknown capability as 'Not checked yet' with the grey ring dot", async () => {
    install({
      user: member,
      rows: [capability("chat", "Chat", "ready"), capability("image", "Images", "unknown")],
    });
    render(
      <AppShell>
        <h1>Models</h1>
      </AppShell>,
    );
    const pills = await screen.findAllByRole("button", {
      name: "Gateway status: Not checked yet",
    });
    const pill = pills[0];
    expect(pill).toHaveTextContent("Not checked yet");
    expect(screen.queryByRole("button", { name: /\bissues?$/ })).toBeNull();
    expect(dotOf(pill).className).toMatch(/border-neutral/);
    expect(dotOf(pill).className).not.toMatch(
      /bg-danger-fill|bg-warning-fill|bg-success-fill/,
    );
  });

  it("reads one Down capability as '1 issue' with the red dot", async () => {
    install({
      user: member,
      rows: [capability("chat", "Chat", "ready"), capability("image", "Images", "down")],
    });
    render(
      <AppShell>
        <h1>Models</h1>
      </AppShell>,
    );
    const pill = (
      await screen.findAllByRole("button", { name: "Gateway status: 1 issue" })
    )[0];
    expect(dotOf(pill).className).toMatch(/bg-danger-fill/);
  });

  it("lists the capability behind the count and links to Status", async () => {
    install({
      user: member,
      rows: [capability("chat", "Chat", "ready"), capability("image", "Images", "unknown")],
    });
    const { popover } = await openPill("Gateway status: Not checked yet");
    expect(
      within(popover)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["Images · Unknown"]);
    expect(within(popover).getByRole("link", { name: "See why" })).toHaveAttribute(
      "href",
      "/status",
    );
  });

  it("explains member health from their allowed models without owner requests", async () => {
    install({
      user: member,
      rows: [capability("chat", "Chat", "ready"), capability("image", "Images", "down")],
    });
    const { popover } = await openPill("Gateway status: 1 issue");
    expect(popover).toHaveTextContent("Your AIImages down");
    expect(popover).toHaveTextContent("GatewayReachable");
    expect(within(popover).getByRole("link", { name: "See why" })).toHaveAttribute(
      "href",
      "/status",
    );
    expect(requested.filter((path) => path.startsWith("/admin/api/"))).toEqual([]);
  });

  it("puts an unreachable gateway before unavailable member models", async () => {
    install({
      user: member,
      rows: [capability("image", "Images", "down")],
      health: () => json({ error: "unavailable" }, 503),
    });
    const { popover } = await openPill("Gateway status: Gateway unreachable");
    expect(popover).toHaveTextContent("GatewayNot reachable");
    expect(within(popover).queryByText("Your AI")).toBeNull();
  });
});

type Mode = "down" | "unobserved" | "disabled" | "unavailable" | "hardware" | "unused" | "unknown-usage";

function ownerStatus(mode: Mode) {
  return {
    current_health: {
      state: "warn",
      tone: "warn",
      label: "Needs attention",
      detail: "",
      observation_available: mode !== "unavailable" && mode !== "unobserved",
      signals: [{ source: "hardware", tone: "bad", reasons: ["Disk is full"] }],
      services:
        mode === "hardware"
          ? []
          : [
              {
                service_id: "image",
                name: "Image engine",
                configured: true,
                enabled: mode !== "disabled",
                in_use: mode === "unknown-usage" ? null : mode !== "unused",
                role: "required",
                routable: false,
                // "unobserved": a stale down row with no current observation behind it.
                observation: mode === "unavailable" ? "unavailable" : "down",
                tone: "bad",
                last_error: "Connection refused",
              },
            ],
    },
  };
}

const ENGINE_ALERT = "Image engine can’t take requests right now.";

describe("owner alert banner", () => {
  it("names a down, enabled, in-use engine and links to its Status section", async () => {
    install({ user: owner, rows: [], status: ownerStatus("down") });
    render(
      <AppShell>
        <h1>Models</h1>
      </AppShell>,
    );
    const banner = (await screen.findByText(ENGINE_ALERT, { exact: false })).closest("p")!;
    expect(banner).toHaveAttribute("role", "status");
    expect(within(banner).getByRole("link", { name: "See Status" })).toHaveAttribute(
      "href",
      "/status#engines",
    );
  });

  it("repeats the engine alert in the owner's health popover", async () => {
    install({ user: owner, rows: [], status: ownerStatus("down") });
    const { popover } = await openPill(/^Gateway status:/);
    expect(popover).toHaveTextContent(`Engines${ENGINE_ALERT}`);
    expect(popover).toHaveTextContent("GatewayReachable");
  });

  it.each(["disabled", "unavailable", "unobserved", "hardware", "unused", "unknown-usage"] as const)(
    "shows no banner for %s",
    async (mode) => {
      install({ user: owner, rows: [], status: ownerStatus(mode) });
      render(
        <AppShell>
          <h1>Models</h1>
        </AppShell>,
      );
      // The status query has landed once the pill leaves "Checking".
      const pill = (
        await screen.findAllByRole("button", { name: /^Gateway status: (?!Checking)/ })
      )[0];
      await userEvent.setup().click(pill);
      await screen.findByText("Your AI");
      await waitFor(() =>
        expect(requested.filter((path) => path === "/admin/api/status").length).toBeGreaterThan(0),
      );
      expect(screen.queryByText(ENGINE_ALERT)).toBeNull();
      expect(screen.queryByRole("status")).toBeNull();
      // Hardware signals never become an owner-wide banner.
      expect(screen.queryByText("Disk is full")).toBeNull();
    },
  );

  it("names an enabled provider that is explicitly unreachable", async () => {
    install({
      user: owner,
      rows: [],
      providers: {
        providers: [
          { name: "Remote provider", enabled: true, reachable: false, key_set: true },
        ],
      },
    });
    render(
      <AppShell>
        <h1>Models</h1>
      </AppShell>,
    );
    const banner = (
      await screen.findByText("Remote provider can’t take requests right now.", { exact: false })
    ).closest("p")!;
    expect(banner).toHaveAttribute("role", "status");
    expect(within(banner).getByRole("link", { name: "See Status" })).toHaveAttribute(
      "href",
      "/status#providers",
    );
  });
});

describe("global interactive cursors", () => {
  let cursorStyle: HTMLStyleElement;

  beforeEach(() => {
    const stylesheet = parse(
      readFileSync("src/app/globals.css", "utf8"),
    );
    const base = stylesheet.nodes.find(
      (node) => node.type === "atrule" && node.name === "layer" && node.params === "base",
    );
    if (!base || base.type !== "atrule") throw new Error("Missing global base layer");
    const rules: string[] = [];
    base.walkDecls("cursor", (declaration) => {
      const rule = declaration.parent;
      if (rule?.type !== "rule") throw new Error("Cursor must belong to a base rule");
      // Priority over utility-layer primitive defaults is intentional.
      expect(declaration.important).toBe(true);
      rules.push(rule.clone({ nodes: [declaration.clone()] }).toString());
    });
    expect(rules).toHaveLength(2);
    cursorStyle = document.createElement("style");
    // jsdom does not consistently support cascade layers or Tailwind @apply.
    // Use the production selectors/declarations unchanged, unwrapped from base.
    // Full compiled utility/layer precedence and pointer hit-testing need a browser.
    cursorStyle.textContent = rules.join("\n");
    document.head.append(cursorStyle);
  });

  afterEach(() => cursorStyle?.remove());

  function expectCursor(element: Element, cursor: string) {
    expect(getComputedStyle(element).cursor, element.outerHTML).toBe(cursor);
  }

  function renderInteractiveKinds() {
    return render(
      <div>
        <button data-testid="native-button">Button</button>
        <div data-testid="role-button" role="button" tabIndex={0}>Role button</div>
        <a data-testid="link" href="#cursor">Link</a>
        <details><summary data-testid="summary">Disclosure</summary></details>
        <label data-testid="label" htmlFor="cursor-checkbox">Checkbox label</label>
        <div role="tablist"><div data-testid="tab" role="tab" tabIndex={0}>Tab</div></div>
        <div role="menu"><div data-testid="menuitem" role="menuitem" tabIndex={0}>Item</div></div>
        <select data-testid="select" aria-label="Select"><option>One</option></select>
        <input data-testid="checkbox" id="cursor-checkbox" type="checkbox" />
        <input data-testid="radio" type="radio" aria-label="Radio" />
      </div>,
    );
  }

  it.each(["aria-disabled", "data-disabled", "disabled"])(
    "covers every interactive kind and restores enabled cursors after %s clears",
    (attribute) => {
      renderInteractiveKinds();
      for (const id of [
        "native-button", "role-button", "link", "summary", "label",
        "tab", "menuitem", "select", "checkbox", "radio",
      ]) {
        const control = screen.getByTestId(id);
        expectCursor(control, "pointer");
        control.setAttribute(attribute, attribute === "aria-disabled" ? "true" : "");
        expectCursor(control, "not-allowed");
        control.removeAttribute(attribute);
        expectCursor(control, "pointer");
        control.setAttribute("aria-disabled", "false");
        expectCursor(control, "pointer");
      }
    },
  );

  it("honors inherited native disabled state and the first-legend exception", () => {
    render(
      <fieldset disabled>
        <legend><button>Legend action</button></legend>
        <button>Fieldset action</button>
        <select aria-label="Fieldset select"><option>One</option></select>
        <input aria-label="Fieldset checkbox" type="checkbox" />
        <input aria-label="Fieldset radio" type="radio" />
      </fieldset>,
    );
    expectCursor(screen.getByRole("button", { name: "Legend action" }), "pointer");
    for (const control of [
      screen.getByRole("button", { name: "Fieldset action" }),
      screen.getByRole("combobox"),
      screen.getByRole("checkbox"),
      screen.getByRole("radio"),
    ]) {
      expectCursor(control, "not-allowed");
    }
  });

  it("applies to real shared product controls, including disabled primitive buttons", () => {
    render(
      <>
        <FilterToggle pressed={false}>Available filter</FilterToggle>
        <FilterToggle pressed={false} disabled>Unavailable filter</FilterToggle>
        <CopyField value="example.test" label="Ready address" />
        <CopyField value="example.test" label="Disabled address" disabled />
        <CopyField value="example.test" label="Loading address" loading />
        <EntityName name="Linked entity" href="/apps" />
        <PageHeader title="Cursor coverage" subtitle="Additional information about this page is available in this disclosure." />
        <Segmented label="Segments" value="one" options={[{ value: "one", label: "Segment one" }]} onChange={() => {}} />
        <CodeBlock code="" language="Empty" />
        <SettingRow label="Shared checkbox label" htmlFor="shared-checkbox" control={<input id="shared-checkbox" type="checkbox" />} />
      </>,
    );
    for (const control of [
      screen.getByRole("button", { name: "Available filter" }),
      screen.getByRole("button", { name: "Copy ready address" }),
      screen.getByRole("button", { name: "Segment one" }),
      screen.getByRole("link", { name: "Linked entity" }),
      screen.getByRole("tab", { name: "Empty" }),
      screen.getByText("About this page"),
      screen.getByText("Shared checkbox label"),
      screen.getByRole("checkbox"),
    ]) {
      expectCursor(control, "pointer");
    }
    for (const name of [
      "Unavailable filter", "Copy disabled address", "Copy loading address", "Copy Empty code",
    ]) {
      expectCursor(screen.getByRole("button", { name }), "not-allowed");
    }
  });

  it("covers real shared menu items with enabled and Radix-disabled markup", () => {
    render(
      <DropdownMenu open modal={false}>
        <DropdownMenuTrigger>Cursor menu</DropdownMenuTrigger>
        <DropdownMenuContent>
          <DropdownMenuItem>Enabled item</DropdownMenuItem>
          <DropdownMenuItem disabled>Disabled item</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>,
    );
    expectCursor(screen.getByRole("button", { name: "Cursor menu" }), "pointer");
    expectCursor(screen.getByRole("menuitem", { name: "Enabled item" }), "pointer");
    expectCursor(screen.getByRole("menuitem", { name: "Disabled item" }), "not-allowed");
  });

  it("does not assign an action cursor to noninteractive content or text inputs", () => {
    render(
      <>
        <a data-testid="no-href">Anchor without a destination</a>
        <label data-testid="no-for">Unassociated label</label>
        <p data-testid="paragraph">Plain text</p>
        <input aria-label="Text entry" type="text" />
      </>,
    );
    for (const element of [
      screen.getByTestId("no-href"),
      screen.getByTestId("no-for"),
      screen.getByTestId("paragraph"),
      screen.getByRole("textbox"),
    ]) {
      expect(getComputedStyle(element).cursor).not.toBe("pointer");
      expect(getComputedStyle(element).cursor).not.toBe("not-allowed");
    }
  });
});
