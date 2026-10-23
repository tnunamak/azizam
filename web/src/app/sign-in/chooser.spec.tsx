import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SignInPage from "./page";
import { continueTo } from "./auth-api";
import { navigate } from "@/lib/navigate";

vi.mock("./auth-api", async (original) => ({
  ...(await original<typeof import("./auth-api")>()),
  continueTo: vi.fn(),
}));

type Person = {
  tile: string;
  name: string;
  avatar: { variant: string; initial: string };
  method: "password" | "sso";
  provider: string | null;
  hint: string | null;
};

const robin: Person = { tile: "t_robin", name: "Robin Okafor", avatar: { variant: "v1", initial: "R" }, method: "password", provider: null, hint: null };
const lea: Person = { tile: "t_lea", name: "Léa Okafor", avatar: { variant: "v4", initial: "L" }, method: "password", provider: null, hint: null };
const nasrin: Person = { tile: "t_nasrin", name: "Nasrin Rahimi-Esfahani", avatar: { variant: "v2", initial: "N" }, method: "password", provider: null, hint: null };
const sam: Person = { tile: "t_sam", name: "Sam Okafor", avatar: { variant: "v3", initial: "S" }, method: "sso", provider: "Family login", hint: null };

const defaults = {
  built_in: true,
  oidc: false,
  oidc_label: "",
  oidc_url: "/login?return_to=%2F",
  instance_name: "Azizam",
};

let options: typeof defaults & { oidc_label: string };
let remembered: { remember: boolean; people: Person[] };
let rememberedReply: () => Promise<Response>;
let loginReply: () => Promise<Response>;
let loginBodies: Record<string, unknown>[];
let deletedTiles: string[];
let client: QueryClient;

const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status }));

beforeEach(() => {
  options = { ...defaults };
  remembered = { remember: true, people: [robin, lea] };
  rememberedReply = () => json(remembered);
  loginReply = () => json({ next: "/" });
  loginBodies = [];
  deletedTiles = [];
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState(null, "", "/sign-in/");
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.startsWith("/auth/api/options")) return json(options);
      if (url === "/setup/api/state") return json({ owner_exists: true });
      if (url === "/auth/api/remembered") return rememberedReply();
      if (url.startsWith("/auth/api/remembered/") && method === "DELETE") {
        deletedTiles.push(decodeURIComponent(url.slice("/auth/api/remembered/".length)));
        return json({ ok: true });
      }
      if (url === "/auth/api/login") {
        loginBodies.push(JSON.parse(String(init?.body)));
        return loginReply();
      }
      return json({});
    }),
  );
});

afterEach(() => {
  cleanup();
  client.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function mount() {
  return render(
    <QueryClientProvider client={client}>
      <SignInPage />
    </QueryClientProvider>,
  );
}

function setAddress(path: string) {
  window.history.replaceState(null, "", path);
}

/** The names of the chooser's rows, in the order they are shown. */
async function rowNames(): Promise<string[]> {
  const list = await screen.findByRole("list");
  return within(list)
    .getAllByRole("button")
    .map((button) => button.getAttribute("aria-labelledby"))
    .map((id) => document.getElementById(id ?? "")?.textContent ?? "");
}

/**
 * The status line once it shows this text. The loading line is a status too, so a bare role lookup can
 * resolve before the page has decided what to say.
 */
async function statusShowing(text: string): Promise<HTMLElement> {
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(text));
  return screen.getByRole("status");
}

describe("the chooser", () => {
  it("lists the remembered people in the gateway's order, then Someone else, and names the page", async () => {
    remembered.people = [robin, lea];
    mount();

    expect(await screen.findByRole("heading", { name: "Who’s signing in?" })).toHaveFocus();
    expect(document.title).toBe("Who’s signing in? · Azizam");
    expect(await rowNames()).toEqual(["Robin Okafor", "Léa Okafor", "Someone else"]);
    expect(screen.getByRole("button", { name: "Forget someone on this browser" })).toBeVisible();
    expect(screen.queryByLabelText("Email")).toBeNull();
  });

  it("never shows a picture on the list", async () => {
    mount();
    await screen.findByRole("heading", { name: "Who’s signing in?" });
    expect(document.querySelector("img")).toBeNull();
  });

  it("shows the plain form, not the list, when only one person is remembered", async () => {
    remembered.people = [robin];
    mount();
    expect(await screen.findByLabelText("Email")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Who’s signing in?" })).toBeNull();
  });

  it("shows the plain form when nobody is remembered", async () => {
    remembered.people = [];
    mount();
    expect(await screen.findByLabelText("Email")).toBeVisible();
  });

  it("shows the plain form without the name box when the owner remembers nobody", async () => {
    remembered = { remember: false, people: [] };
    mount();
    expect(await screen.findByLabelText("Email")).toBeVisible();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("a refused sign-in shows the sign-out screen in place of the list (S8)", async () => {
    setAddress("/sign-in/?error=not_fresh&provider_sign_out=1");
    options.oidc = true;
    options.oidc_label = "Keycloak";
    mount();
    expect(
      await screen.findByRole("heading", { level: 1, name: "Sign out of Keycloak first" }),
    ).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Who’s signing in?" })).toBeNull();
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("a stale single sign-on name sends the person back with the notice above the list", async () => {
    setAddress("/sign-in/?error=tile_unknown");
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That name can’t sign in from this list. Try another way to sign in, or ask the person who invited you for help.",
    );
    expect(await rowNames()).toContain("Robin Okafor");
  });

  it("offers a plain message and Try again when the list cannot be reached", async () => {
    rememberedReply = () => Promise.reject(new TypeError("Failed to fetch"));
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t reach the gateway.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
    expect(screen.getAllByRole("heading")).toHaveLength(1);
  });

  it("falls back to the plain form when the gateway refuses the list read", async () => {
    rememberedReply = () => json({ error: { code: "login_rate_limited", message: "Too many requests." } }, 429);
    mount();
    expect(await screen.findByLabelText("Email")).toBeVisible();
  });
});

describe("after a switch or a sign-out", () => {
  it("says who was signed out and sends that person to the end of the list", async () => {
    setAddress("/sign-in/?after=switch");
    sessionStorage.setItem("azizam.handoff.last", JSON.stringify({ tile: "t_robin" }));
    remembered.people = [robin, lea];
    mount();

    const status = await statusShowing("Robin Okafor is signed out. To come back, sign in again.");
    expect(status).toHaveTextContent("Robin Okafor is signed out. To come back, sign in again.");
    expect(await rowNames()).toEqual(["Léa Okafor", "Robin Okafor", "Someone else"]);
  });

  it("says only that the person is signed out when the one who left is not on the list", async () => {
    setAddress("/sign-in/?after=switch");
    sessionStorage.setItem("azizam.handoff.last", JSON.stringify({ tile: "t_sam" }));
    mount();
    await statusShowing("You’re signed out. To come back, sign in again.");
  });

  it("takes the ending out of the address, so a reload shows no status line", async () => {
    setAddress("/sign-in/?after=switch");
    mount();
    await screen.findByRole("heading", { name: "Who’s signing in?" });
    expect(window.location.search).toBe("");
  });

  it("offers to forget the person who signed out, and forgets only that name", async () => {
    setAddress("/sign-in/?after=sign-out");
    sessionStorage.setItem("azizam.handoff.last", JSON.stringify({ tile: "t_lea" }));
    mount();

    const status = await statusShowing("You’re signed out.");
    const forget = within(status).getByRole("button", { name: "Forget Léa Okafor on this browser" });
    const user = userEvent.setup();
    await user.click(forget);

    await waitFor(() => expect(deletedTiles).toEqual(["t_lea"]));
    await statusShowing("Léa Okafor is forgotten on this browser.");
    // One name is left, so the form replaces the list.
    expect(await screen.findByLabelText("Email")).toBeVisible();
  });

  it("offers no forget line when the person who signed out is not on the list", async () => {
    setAddress("/sign-in/?after=sign-out");
    sessionStorage.setItem("azizam.handoff.last", JSON.stringify({ tile: "t_sam" }));
    remembered.people = [robin, lea];
    mount();
    const status = await statusShowing("You’re signed out.");
    expect(within(status).queryByRole("button")).toBeNull();
  });

  it("confirms a sign-out everywhere", async () => {
    setAddress("/sign-in/?after=everywhere");
    mount();
    await statusShowing("You’re signed out everywhere.");
  });

  it("names the provider after a provider sign-out, and asks for a name", async () => {
    setAddress("/sign-in/?after=provider-sign-out");
    options.oidc = true;
    options.oidc_label = "Family login";
    mount();
    await statusShowing("You’re signed out of Family login. Choose your name to sign in again.");
  });

  it("drops the link to the last page: the options and the sign-in both go Home", async () => {
    setAddress("/sign-in/?after=switch&return_to=%2Fusage%2F");
    mount();
    await screen.findByRole("heading", { name: "Who’s signing in?" });
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/auth/api/options?return_to=%2F",
        expect.anything(),
      ),
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Someone else" }));
    await user.type(await screen.findByLabelText("Email"), "member@example.test");
    await user.type(screen.getByLabelText("Password"), "A long test password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(loginBodies[0]).toMatchObject({ return_to: "/" }));
  });

  it("drops a link to an earlier person's page after a sign-out, until someone signs in", async () => {
    localStorage.setItem(
      "azizam.handoff",
      JSON.stringify({ state: "SIGNED_OUT", since: 1, kind: "sign-out" }),
    );
    setAddress("/sign-in/?return_to=%2Fusage%2F");
    remembered.people = [];
    mount();
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Email"), "member@example.test");
    await user.type(screen.getByLabelText("Password"), "A long test password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(loginBodies[0]).toMatchObject({ return_to: "/" }));
  });
});

describe("forget mode", () => {
  beforeEach(() => {
    remembered.people = [robin, lea, nasrin];
  });

  it("lists names with a Forget button each, and no row is a button", async () => {
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Forget someone on this browser" }));

    expect(await screen.findByRole("heading", { name: "Forget someone on this browser" })).toHaveFocus();
    expect(document.title).toBe("Forget someone on this browser · Azizam");
    expect(screen.getByText("Only their name comes off this list. They can still sign in.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Robin Okafor" })).toBeNull();
    expect(screen.getByRole("button", { name: "Forget Robin Okafor on this browser" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Forget Léa Okafor on this browser" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Done" })).toBeVisible();
  });

  it("moves focus to the next row's Forget button after a forget, and reports the name", async () => {
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Forget someone on this browser" }));
    await user.click(await screen.findByRole("button", { name: "Forget Robin Okafor on this browser" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Forget Léa Okafor on this browser" })).toHaveFocus(),
    );
    expect(deletedTiles).toEqual(["t_robin"]);
    expect(screen.getByRole("status")).toHaveTextContent("Robin Okafor is forgotten on this browser.");
  });

  it("moves focus to Done when the last row is forgotten", async () => {
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Forget someone on this browser" }));
    await user.click(await screen.findByRole("button", { name: "Forget Nasrin Rahimi-Esfahani on this browser" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Done" })).toHaveFocus());
  });

  it("returns to the list from Done while two or more names remain", async () => {
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Forget someone on this browser" }));
    await user.click(await screen.findByRole("button", { name: "Forget Robin Okafor on this browser" }));
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(await rowNames()).toEqual(["Léa Okafor", "Nasrin Rahimi-Esfahani", "Someone else"]);
  });

  it("returns to the form from Done when fewer than two names remain", async () => {
    remembered.people = [robin, lea];
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Forget someone on this browser" }));
    await user.click(await screen.findByRole("button", { name: "Forget Robin Okafor on this browser" }));
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(await screen.findByLabelText("Email")).toBeVisible();
  });

  it("says everyone is forgotten on the form once the last name goes", async () => {
    remembered.people = [robin, lea];
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Forget someone on this browser" }));
    await user.click(await screen.findByRole("button", { name: "Forget Robin Okafor on this browser" }));
    await user.click(await screen.findByRole("button", { name: "Forget Léa Okafor on this browser" }));
    expect(await screen.findByLabelText("Email")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Everyone is forgotten on this browser.");
  });
});

describe("password sign-in for a remembered person", () => {
  beforeEach(() => {
    remembered.people = [robin, lea, nasrin];
  });

  async function openLea() {
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Léa Okafor" }));
    return user;
  }

  it("names the page for the person, and focuses a password field labelled for them", async () => {
    await openLea();
    expect(await screen.findByRole("heading", { name: "Léa Okafor" })).toBeVisible();
    expect(document.title).toBe("Léa Okafor · Sign in · Azizam");
    const password = screen.getByLabelText("Password for Léa Okafor");
    await waitFor(() => expect(password).toHaveFocus());
    expect(screen.queryByLabelText("Email")).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("returns to the list from 'Not you? Choose someone else'", async () => {
    const user = await openLea();
    await user.click(await screen.findByRole("button", { name: "Not you? Choose someone else" }));
    expect(await rowNames()).toEqual(["Robin Okafor", "Léa Okafor", "Nasrin Rahimi-Esfahani", "Someone else"]);
  });

  it("signs the person in by tile, asks to keep the name, and sends no email", async () => {
    const user = await openLea();
    await user.type(screen.getByLabelText("Password for Léa Okafor"), "A long test password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(continueTo).toHaveBeenCalledWith("/"));
    expect(loginBodies).toEqual([{ tile: "t_lea", password: "A long test password", remember: true }]);
  });

  it("marks only the password field for a wrong password, and keeps focus there with its text selected", async () => {
    loginReply = () => json({ error: { code: "invalid_login", message: "Wrong" } }, 401);
    const user = await openLea();
    const password = screen.getByLabelText("Password for Léa Okafor");
    await user.type(password, "wrong one");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("That password isn’t right. Try again.");
    expect(password).toHaveAttribute("aria-invalid", "true");
    expect(password).toHaveFocus();
    expect((password as HTMLInputElement).selectionStart).toBe(0);
    expect((password as HTMLInputElement).selectionEnd).toBe("wrong one".length);
  });

  it("says too many tries above the button, with the wait, and does not mark the field", async () => {
    loginReply = () =>
      json({ error: { code: "login_rate_limited", message: "Too many attempts. Try again in 15 minutes." } }, 429);
    const user = await openLea();
    const password = screen.getByLabelText("Password for Léa Okafor");
    await user.type(password, "A long test password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many tries. Wait 15 minutes, then try again.",
    );
    expect(password).not.toHaveAttribute("aria-invalid");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("says the gateway could not be reached, without marking the field", async () => {
    loginReply = () => Promise.reject(new TypeError("Failed to fetch"));
    const user = await openLea();
    await user.type(screen.getByLabelText("Password for Léa Okafor"), "A long test password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t reach the gateway. Try again in a moment.");
    expect(screen.getByLabelText("Password for Léa Okafor")).not.toHaveAttribute("aria-invalid");
  });

  it("says the browser is offline when it knows it is", async () => {
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
    loginReply = () => Promise.reject(new TypeError("Failed to fetch"));
    const user = await openLea();
    await user.type(screen.getByLabelText("Password for Léa Okafor"), "A long test password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("You’re offline. Try again when you’re connected.");
  });

  it("explains a person this list can no longer sign in, and removes their row", async () => {
    loginReply = () => json({ error: { code: "tile_unknown", message: "That name is no longer on this browser." } }, 404);
    const user = await openLea();
    await user.type(screen.getByLabelText("Password for Léa Okafor"), "A long test password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { name: "Léa Okafor can’t sign in from this list" })).toBeVisible();
    expect(screen.getByText("Try another way to sign in, or ask the person who invited you for help.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Back to the list" }));
    expect(await rowNames()).toEqual(["Robin Okafor", "Nasrin Rahimi-Esfahani", "Someone else"]);
  });

  it("starts a single sign-on person's sign-in by tile with a fresh login, and keeps focus on that row", async () => {
    remembered.people = [robin, sam];
    const navigateTo = vi.spyOn(navigate, "to").mockImplementation(() => {});
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Sam Okafor" }));

    await statusShowing("Opening Family login");
    expect(screen.getByRole("button", { name: "Robin Okafor" })).toHaveAttribute("aria-disabled", "true");
    expect(navigateTo).toHaveBeenCalledWith("/login?tile=t_sam&remember=1");
  });
});

describe("the plain form", () => {
  it("shows the name box on by default, under the heading, and sends remember true", async () => {
    remembered.people = [];
    mount();
    const box = await screen.findByRole("checkbox", { name: "Show my name on this browser next time" });
    expect(box).toBeChecked();
    expect(screen.getByText("Turn this off on a public computer.")).toBeVisible();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Email"), "member@example.test");
    await user.type(screen.getByLabelText("Password"), "A long test password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(loginBodies[0]).toMatchObject({ remember: true }));
  });

  it("sends remember false when the name box is unticked", async () => {
    remembered.people = [];
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("checkbox", { name: "Show my name on this browser next time" }));
    await user.type(screen.getByLabelText("Email"), "member@example.test");
    await user.type(screen.getByLabelText("Password"), "A long test password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(loginBodies[0]).toMatchObject({ remember: false }));
  });

  it("asks the single sign-on start to remember the name, or not, with the box", async () => {
    remembered.people = [];
    options.oidc = true;
    options.oidc_label = "Keycloak";
    mount();
    const link = await screen.findByRole("link", { name: "Continue with Keycloak" });
    expect(link).toHaveAttribute("href", "/login?return_to=%2F&remember=1");
    const user = userEvent.setup();
    await user.click(screen.getByRole("checkbox"));
    expect(link).toHaveAttribute("href", "/login?return_to=%2F&remember=0");
  });

  it("has no name box when the owner remembers nobody", async () => {
    remembered = { remember: false, people: [] };
    options.oidc = true;
    options.oidc_label = "Keycloak";
    mount();
    expect(await screen.findByRole("link", { name: "Continue with Keycloak" })).toHaveAttribute(
      "href",
      "/login?return_to=%2F&remember=0",
    );
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("opens from Someone else with a Back to the list link, and keeps the link to an invitation", async () => {
    setAddress("/sign-in/?return_to=%2Finvite%2F");
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Someone else" }));
    expect(await screen.findByRole("button", { name: "Back to the list" })).toBeVisible();
    await user.type(screen.getByLabelText("Email"), "invitee@example.test");
    await user.type(screen.getByLabelText("Password"), "A long test password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(loginBodies[0]).toMatchObject({ return_to: "/invite/" }));
  });

  it("goes back to the list from Back to the list", async () => {
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Someone else" }));
    await user.click(await screen.findByRole("button", { name: "Back to the list" }));
    expect(await rowNames()).toEqual(["Robin Okafor", "Léa Okafor", "Someone else"]);
  });
});
