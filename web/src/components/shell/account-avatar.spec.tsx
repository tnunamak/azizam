import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppShell } from "./app-shell";
import { fetchViewer } from "./session";
import { resetSessionCache } from "@/lib/api";
import { AVATAR_PICTURE_PATH, generatedAvatarPalettes, getGeneratedAvatar } from "@/components/product/person-avatar";

// The shell keeps one query client for the page; the tests empty it between cases.
const clients = vi.hoisted(() => [] as QueryClient[]);
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    createQueryClient: () => {
      const client = actual.createQueryClient();
      const defaults = client.getDefaultOptions();
      client.setDefaultOptions({ ...defaults, queries: { ...defaults.queries, retry: false } });
      clients.push(client);
      return client;
    },
  };
});
vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("./use-after-first-load", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./use-after-first-load")>()),
  useAfterFirstLoad: () => true,
}));

const PICTURE = `${AVATAR_PICTURE_PATH}?v=1760000000`;
const PERSON = "usr_3f9a1c2b";

type Snapshot = {
  authenticated: boolean;
  csrf_token: string;
  user: Record<string, unknown>;
  apps: unknown[];
};

function snapshot(avatar: { choice: string; picture_url: string | null }, name = "Alex Owner"): Snapshot {
  return {
    authenticated: true,
    csrf_token: "csrf-token",
    user: {
      id: PERSON,
      person_id: PERSON,
      name,
      email: "owner@example.test",
      role: "owner",
      avatar: { ...avatar, person_id: PERSON },
    },
    apps: [],
  };
}

const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }));

let current: Snapshot;
let saves: { body: unknown; headers: Headers }[];
let saveResponse: (choice: string) => Promise<Response>;

beforeEach(() => {
  clients.forEach((client) => client.clear());
  resetSessionCache();
  current = snapshot({ choice: "auto", picture_url: null });
  saves = [];
  saveResponse = (choice) =>
    json({ choice, person_id: PERSON, picture_url: choice === "picture" ? PICTURE : null });
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false, media: query, onchange: null, addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    })),
  });
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  HTMLCanvasElement.prototype.getContext = vi.fn(() => null);
  Element.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/me/api/session")) return json(current);
      if (url.endsWith("/me/api/profile/avatar") && init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        saves.push({ body, headers: new Headers(init.headers) });
        return saveResponse(body.choice);
      }
      if (url.endsWith("/health")) return json({ status: "healthy" });
      if (url.includes("/.well-known/azizam")) return json({ service: { name: "Kitchen Gateway" }, recipes: {} });
      return json({});
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function openAvatarDialog(name = "Alex Owner") {
  const user = userEvent.setup();
  render(<AppShell><h1>Home</h1></AppShell>);
  await user.click((await screen.findAllByRole("button", { name: `Account menu: ${name}` }))[0]);
  await user.click(await screen.findByRole("menuitem", { name: "Change avatar" }));
  return { user, dialog: await screen.findByRole("dialog", { name: "Your avatar" }) };
}

describe("the viewer", () => {
  it("carries the person id and the avatar from the session", async () => {
    current = snapshot({ choice: "v3", picture_url: PICTURE });
    vi.mocked(fetch);
    const viewer = await fetchViewer();
    expect(viewer.id).toBe(PERSON);
    expect(viewer.avatar).toEqual({ choice: "v3", picture_url: PICTURE });
  });

  it("has no avatar when the session sends none (an older gateway)", async () => {
    delete current.user.avatar;
    delete current.user.person_id;
    const viewer = await fetchViewer();
    expect(viewer.avatar).toBeUndefined();
    expect(viewer.id).toBe(PERSON);
  });
});

describe("account menu avatar", () => {
  it("draws the account button from the person id, so a rename keeps the colour", async () => {
    const button = async (name: string) => {
      current = snapshot({ choice: "auto", picture_url: null }, name);
      clients.forEach((client) => client.clear());
      resetSessionCache();
      const view = render(<AppShell><h1>Home</h1></AppShell>);
      const [trigger] = await screen.findAllByRole("button", { name: `Account menu: ${name}` });
      const disc = trigger.querySelector<HTMLElement>("[data-avatar-selection] > span")!;
      const background = disc.style.backgroundImage;
      view.unmount();
      return background;
    };
    const before = await button("Alex Owner");
    const after = await button("Sam Rivera");
    expect(before).toBe(after);
    expect(before).toBe(getGeneratedAvatar(PERSON).background);
  });

  it("adds Change avatar to the menu and opens a labelled dialog with plain words", async () => {
    const { dialog } = await openAvatarDialog();
    expect(within(dialog).getByText("Choose how you appear to other people.")).toBeVisible();
    expect(within(dialog).getByRole("group", { name: "Choose your avatar" })).toBeVisible();
    expect(within(dialog).getAllByRole("radio")).toHaveLength(8);
    expect(within(dialog).getByRole("button", { name: "Done" })).toBeVisible();
    expect(within(dialog).queryByRole("radio", { name: /sign-in picture/ })).toBeNull();
  });

  it("saves the chosen avatar with the user-action header and the CSRF token, then shows it", async () => {
    const { user, dialog } = await openAvatarDialog();
    const target = generatedAvatarPalettes.find((palette) => palette.id !== getGeneratedAvatar(PERSON).id)!;
    await user.click(within(dialog).getByRole("radio", { name: new RegExp(target.label) }));
    await waitFor(() => expect(saves).toHaveLength(1));
    expect(saves[0].body).toEqual({ choice: target.id });
    expect(saves[0].headers.get("x-azizam-user-action")).toBe("1");
    expect(saves[0].headers.get("x-azizam-csrf")).toBe("csrf-token");
    await waitFor(() => expect(within(dialog).getByRole("radio", { name: new RegExp(target.label) })).toBeChecked());
    const preview = dialog.querySelector<HTMLElement>("[data-slot='person-avatar'] > span")!;
    expect(preview.style.backgroundImage).toBe(getGeneratedAvatar(PERSON, target.id).background);
  });

  it("offers the sign-in picture when the provider shared one, and saves it as the word picture", async () => {
    current = snapshot({ choice: "auto", picture_url: PICTURE });
    const { user, dialog } = await openAvatarDialog();
    await user.click(within(dialog).getByRole("radio", { name: "Use my sign-in picture" }));
    await waitFor(() => expect(saves).toEqual([expect.objectContaining({ body: { choice: "picture" } })]));
    await waitFor(() => expect(within(dialog).getByRole("radio", { name: "Use my sign-in picture" })).toBeChecked());
  });

  it("says plainly when it could not save, and keeps the old choice", async () => {
    saveResponse = () => json({ error: { code: "x", message: "internal detail" } }, 500);
    const { user, dialog } = await openAvatarDialog();
    const before = within(dialog).getAllByRole("radio").find((radio) => (radio as HTMLInputElement).checked)!;
    const other = within(dialog).getAllByRole("radio").find((radio) => radio !== before)!;
    await user.click(other);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("We couldn't save your avatar. Try again.");
    expect(within(dialog).queryByText(/internal detail/)).toBeNull();
    expect(before).toBeChecked();
  });

  it("shows the gateway's own sentence when the picture is no longer available", async () => {
    current = snapshot({ choice: "auto", picture_url: PICTURE });
    saveResponse = () =>
      json({ error: { code: "picture_unavailable", message: "Your sign-in provider has not shared a picture." } }, 409);
    const { user, dialog } = await openAvatarDialog();
    await user.click(within(dialog).getByRole("radio", { name: "Use my sign-in picture" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Your sign-in provider has not shared a picture.");
  });

  it("closes with Done and returns focus to the account button", async () => {
    const { user, dialog } = await openAvatarDialog();
    await user.click(within(dialog).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getAllByRole("button", { name: "Account menu: Alex Owner" })[0]),
    );
  });

  it("keeps the picker out of the menu for a service admin key (no person)", async () => {
    current = { authenticated: false, csrf_token: "", user: {}, apps: [] };
    vi.mocked(fetch).mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/me/api/session")) return json(current);
      if (url.includes("/admin/api/session"))
        return json({ actor: { kind: "service_account", label: "ops-key", email: null, roles: ["owner"], csrf_token: "x" } });
      return json({});
    });
    const user = userEvent.setup();
    render(<AppShell><h1>Home</h1></AppShell>);
    await user.click((await screen.findAllByRole("button", { name: "Account menu: ops-key" }))[0]);
    await screen.findByRole("menuitem", { name: "Help" });
    expect(screen.queryByRole("menuitem", { name: "Change avatar" })).toBeNull();
  });
});
