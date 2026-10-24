import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  test,
  vi,
} from "vitest";

import { ViewerContext, type Viewer } from "@/components/shell/session";
import { accountApi, type GatewayDiscovery } from "@/lib/account-api";
import { productApi } from "@/lib/product-api";
import type { ModelsCatalog } from "@/lib/product-api-types";
import { appGuides } from "@/lib/app-guides";
import { listTrustedAddresses } from "@/lib/trusted-addresses";
import ConnectPage from "./page";

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: React.ComponentProps<"a">) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));
// Only the owner's view loads an App's trusted addresses; this stands in for the admin route.
vi.mock("@/lib/trusted-addresses", () => ({
  listTrustedAddresses: vi.fn(),
  trustedAddressesKey: (appId: string) => [
    "admin",
    "app-trusted-addresses",
    appId,
  ],
}));

const member: Viewer = {
  authenticated: true,
  name: "Sam",
  email: null,
  role: "member",
  admin: false,
};
const owner: Viewer = { ...member, name: "Tim", role: "owner", admin: true };
const signedOut: Viewer = { ...member, authenticated: false };

const discovery: GatewayDiscovery = {
  recipes: {
    openai: { canonical_base_url: "https://gw.example/openai/v1" },
    anthropic: { canonical_base_url: "https://gw.example/anthropic" },
    ollama: { canonical_base_url: "https://gw.example/ollama" },
  },
  mcp: {
    url: "https://gw.example/mcp",
    transport: "streamable-http",
    tools: [
      {
        name: "tool_alpha",
        title: "Alpha",
        description: "Does the alpha thing.",
      },
    ],
  },
};

function renderPage(viewer: Viewer) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider value={viewer}>
        <ConnectPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}

const catalog = {
  aliases: [
    {
      id: "house-chat",
      capability: "chat",
      capabilities: [{ capability: "chat" }],
      recommended: true,
    },
    {
      id: "house-whisper",
      capability: "transcribe",
      capabilities: [{ capability: "transcribe" }],
    },
  ],
  models: [],
} as unknown as ModelsCatalog;

beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.scrollIntoView ??= () => undefined;
});

beforeEach(() => {
  vi.spyOn(accountApi, "mcpConnections").mockResolvedValue([]);
  vi.spyOn(productApi, "models").mockResolvedValue(catalog);
  vi.mocked(listTrustedAddresses).mockReset().mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/** The client picker: every client in the data, as toggles. */
async function picker() {
  return within(await screen.findByRole("group", { name: "Setting up in" }));
}

describe("Connect", () => {
  test("shows a loading state while connection details load", () => {
    vi.spyOn(accountApi, "discovery").mockReturnValue(new Promise(() => {}));
    renderPage(member);
    expect(screen.getByRole("status")).toHaveTextContent(
      /loading connection details/i,
    );
  });

  test("asks a signed-out visitor to sign in and shows no details", () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
    renderPage(signedOut);
    expect(screen.getByText("Sign in to connect your apps")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute(
      "href",
      "/sign-in/?return_to=%2Fconnect%2F",
    );
    expect(screen.queryByLabelText("OpenAI-compatible address")).toBeNull();
  });

  test("offers a retry when details fail to load", async () => {
    vi.spyOn(accountApi, "discovery").mockRejectedValue(new Error("down"));
    renderPage(member);
    expect(
      await screen.findByText("The gateway couldn't load connection details."),
    ).toBeTruthy();
  });

  test("shows a member the endpoints, with the OpenAI-compatible one marked, and a link to Apps & keys", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
    renderPage(member);
    expect(
      await screen.findByLabelText("OpenAI-compatible address"),
    ).toHaveValue("https://gw.example/openai/v1");
    expect(screen.getByText("Most apps use this one.")).toBeTruthy();
    expect(screen.getByLabelText("Anthropic-compatible address")).toHaveValue(
      "https://gw.example/anthropic",
    );
    expect(screen.getByLabelText("Ollama-compatible address")).toHaveValue(
      "https://gw.example/ollama",
    );
    expect(screen.getByLabelText("MCP server address")).toHaveValue(
      "https://gw.example/mcp",
    );
    expect(
      screen.getByRole("link", { name: "choose the app in Apps & keys" }),
    ).toHaveAttribute("href", "/apps/");
    expect(screen.queryByRole("link", { name: "Create a key" })).toBeNull();
    expect(screen.queryByText("Gateway automation")).toBeNull();
    // An address on a public host works from anywhere, so the local-only line stays off.
    expect(
      screen.queryByText("It works only from this machine or this network."),
    ).toBeNull();
  });

  test("shows an owner the automation links as well", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
    renderPage(owner);
    expect(await screen.findByText("Gateway automation")).toBeTruthy();
    expect(screen.getByRole("link", { name: "API reference" })).toHaveAttribute(
      "href",
      "/docs",
    );
  });

  test("lists every client in the picker, and the open client's values with a link to its full steps", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
    renderPage(member);
    const options = within(
      await screen.findByRole("group", { name: "Setting up in" }),
    );
    expect(options.getAllByRole("button")).toHaveLength(appGuides.apps.length);
    expect(
      options.getByRole("button", { name: "Open WebUI", pressed: true }),
    ).toBeTruthy();
    expect(screen.getByLabelText("URL")).toHaveValue(
      "https://gw.example/openai/v1",
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Model IDs")).toHaveValue("house-chat"),
    );
    // The key is never in the page; the key hint says where it goes.
    expect(screen.getByText(/Paste a key from/)).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "Full steps for Open WebUI →" }),
    ).toHaveAttribute("href", "https://azizam.dev/docs/connect/open-webui/");
    expect(document.body.innerHTML).not.toMatch(/opk_|oak_/);
  });

  test("changes the values when another client is chosen", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
    renderPage(member);
    fireEvent.click(
      (await picker()).getByRole("button", { name: "OpenWhispr" }),
    );
    expect(
      (await picker()).getByRole("button", {
        name: "OpenWhispr",
        pressed: true,
      }),
    ).toBeTruthy();
    // OpenWhispr has two groups, each with its own Endpoint URL; both point at the same address.
    for (const field of screen.getAllByLabelText("Endpoint URL")) {
      expect(field).toHaveValue("https://gw.example/openai/v1");
    }
    expect(
      screen.getByRole("link", { name: "Full steps for OpenWhispr →" }),
    ).toHaveAttribute("href", "https://azizam.dev/docs/connect/openwhispr/");
  });

  test("finds a client by name and says so when none matches", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
    renderPage(member);
    const search = await screen.findByRole("searchbox", {
      name: "Find an app",
    });
    fireEvent.change(search, { target: { value: "whispr" } });
    expect((await picker()).getAllByRole("button")).toHaveLength(1);
    fireEvent.change(search, { target: { value: "zzz" } });
    expect(screen.getByText("No app matches “zzz”.")).toBeTruthy();
  });

  test("shows an MCP-only client its server address, how it signs in, and the MCP docs", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
    renderPage(member);
    fireEvent.click((await picker()).getByRole("button", { name: "ChatGPT" }));
    const recipe = await screen.findByRole("region", {
      name: "Values for ChatGPT",
    });
    expect(within(recipe).getByLabelText("MCP server")).toHaveValue(
      "https://gw.example/mcp",
    );
    expect(
      screen.getByText("ChatGPT signs in with your account. It takes no key."),
    ).toBeTruthy();
    expect(screen.getByRole("link", { name: "MCP docs →" })).toHaveAttribute(
      "href",
      "https://azizam.dev/docs/mcp/",
    );
  });

  test("keeps the setup steps, the tool list and the JSON out of the page", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
    renderPage(member);
    await screen.findByLabelText("OpenAI-compatible address");
    expect(screen.queryByText("MCP tools this gateway offers")).toBeNull();
    expect(screen.queryByText("MCP notes by app")).toBeNull();
    expect(screen.queryByRole("list", { name: "How to connect" })).toBeNull();
    expect(screen.queryByText(/"mcpServers"/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Copy JSON code" })).toBeNull();
  });

  test("says that sign-in apps need only the MCP address when sign-in works", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue({
      ...discovery,
      mcp: { ...discovery.mcp!, sign_in: true },
    });
    renderPage(member);
    expect(
      await screen.findByText(/Sign-in apps need only the MCP server address/),
    ).toBeTruthy();
    expect(screen.queryByText(/public https/)).toBeNull();
  });

  test("explains that sign-in needs the public address when it is not set", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue({
      ...discovery,
      mcp: { ...discovery.mcp!, sign_in: false },
    });
    renderPage(owner);
    expect(
      await screen.findByText(/needs this gateway's public https/),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "Set it in Settings" }),
    ).toHaveAttribute("href", "/settings");
  });

  test("marks an address on this machine or network as local only", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue({
      ...discovery,
      recipes: {
        ...discovery.recipes,
        openai: { canonical_base_url: "http://192.168.1.50:5000/openai/v1" },
      },
    });
    renderPage(member);
    expect(
      await screen.findByText(
        "It works only from this machine or this network.",
      ),
    ).toBeTruthy();
    expect(screen.getByLabelText("OpenAI-compatible address")).toHaveValue(
      "http://192.168.1.50:5000/openai/v1",
    );
  });

  test("does not list sign-in connections: Apps & keys is the one list of apps", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
    vi.spyOn(accountApi, "mcpConnections").mockResolvedValue([
      {
        id: "mcg_1",
        client_name: "Hosted Chat",
        client_id: "https://hosted.example/client.json",
        redirect_host: "hosted.example",
        created_at: 1_800_000_000,
        last_used_at: null,
      },
    ]);
    renderPage(member);
    await screen.findByLabelText("OpenAI-compatible address");
    expect(screen.queryByRole("list", { name: "Connections" })).toBeNull();
    expect(screen.queryByText("Connected with sign-in")).toBeNull();
    expect(screen.queryByText("Hosted Chat")).toBeNull();
  });

  describe("with ?app=<id>", () => {
    const savedApp = {
      id: "saved-app",
      name: "OpenWhispr laptop",
      kind: "personal",
      disabled_at: null,
      created_at: 1_800_000_000,
      access_plan: {
        id: "plan_local",
        name: "Local only",
        summary: "Chat.",
        same_as_person: false,
      },
      can_mint_keys: true,
      credentials: [
        {
          id: "key-1",
          prefix: "opk_test1234abcd",
          display_name: "Laptop",
          created_at: 1_800_000_000,
          last_used_at: 1_800_100_000,
        },
      ],
    };
    const zeroKeyApp = {
      ...savedApp,
      id: "zero-app",
      name: "Phone",
      credentials: [],
    };
    const sessionWith = (apps: unknown[]) =>
      vi.spyOn(accountApi, "session").mockResolvedValue({
        authenticated: true,
        csrf_token: "test",
        user: {
          id: "u",
          email: "sam@example.test",
          name: "Sam",
          role: "member",
        },
        apps,
      } as never);

    afterEach(() => {
      window.history.replaceState({}, "", "/");
    });

    test("opens the App's access and newest key first, with the picker set to its client", async () => {
      window.history.replaceState({}, "", "/connect/?app=saved-app");
      vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
      sessionWith([savedApp]);
      renderPage(member);
      expect(
        await screen.findByRole("heading", {
          level: 1,
          name: "Connect OpenWhispr laptop",
        }),
      ).toBeVisible();
      const strip = screen.getByRole("region", {
        name: "OpenWhispr laptop: access and key",
      });
      expect(within(strip).getByText("Access plan: Local only")).toBeVisible();
      expect(within(strip).getByText("opk_test…")).toBeVisible();
      expect(within(strip).getByText(/Last used/)).toBeVisible();
      expect(
        within(strip).getByRole("link", { name: "Rotate key" }),
      ).toHaveAttribute("href", "/apps/?app=saved-app&action=rotate");
      expect(
        screen.getByRole("link", { name: "Back to Apps & keys" }),
      ).toHaveAttribute("href", "/apps/");
      expect(
        (await picker()).getByRole("button", {
          name: "OpenWhispr",
          pressed: true,
        }),
      ).toBeTruthy();
      expect(
        screen.getAllByText("Paste the key you saved for this app.")[0],
      ).toBeVisible();
    });

    test("an App with no keys says so and offers Create key, never only a key to paste", async () => {
      window.history.replaceState({}, "", "/connect/?app=zero-app");
      vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
      sessionWith([zeroKeyApp]);
      renderPage(member);
      const strip = await screen.findByRole("region", {
        name: "Phone: access and key",
      });
      expect(within(strip).getByText("No keys yet")).toBeVisible();
      expect(
        within(strip).getByRole("link", { name: "Create key" }),
      ).toHaveAttribute("href", "/apps/?app=zero-app&action=create-key");
      expect(
        screen.getByText("No key yet. Create one above, then paste it here."),
      ).toBeVisible();
    });

    test("an owner sees the App's keyless addresses on the strip, and a member never asks for them", async () => {
      window.history.replaceState({}, "", "/connect/?app=saved-app");
      vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
      vi.mocked(listTrustedAddresses).mockResolvedValue([
        {
          id: "t1",
          app_id: "saved-app",
          address: "192.168.1.50",
          note: null,
          created_at: 1,
          created_by: null,
        },
      ]);
      sessionWith([savedApp]);
      renderPage(owner);
      expect(
        await screen.findByText("From 192.168.1.50 no key is needed"),
      ).toBeVisible();
    });

    test("a member gets no keyless line and never loads an App's addresses", async () => {
      window.history.replaceState({}, "", "/connect/?app=saved-app");
      vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
      sessionWith([savedApp]);
      renderPage(member);
      await screen.findByRole("region", {
        name: "OpenWhispr laptop: access and key",
      });
      expect(screen.queryByText(/no key is needed/)).toBeNull();
      expect(listTrustedAddresses).not.toHaveBeenCalled();
    });

    test("says so when the app is not in the list, and the page still works", async () => {
      window.history.replaceState({}, "", "/connect/?app=gone");
      vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
      sessionWith([savedApp]);
      renderPage(member);
      expect(
        await screen.findByText(/That app isn't in your list/),
      ).toBeVisible();
      expect(screen.getByLabelText("OpenAI-compatible address")).toHaveValue(
        "https://gw.example/openai/v1",
      );
    });

    test("a plain visit names no app and loads no session", async () => {
      vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
      const session = vi.spyOn(accountApi, "session");
      renderPage(member);
      expect(
        await screen.findByLabelText("OpenAI-compatible address"),
      ).toBeTruthy();
      expect(
        screen.queryByRole("link", { name: "Back to Apps & keys" }),
      ).toBeNull();
      expect(session).not.toHaveBeenCalled();
    });
  });
});
