import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { ViewerContext, type Viewer } from "@/components/shell/session";
import { accountApi, type GatewayDiscovery } from "@/lib/account-api";
import ConnectPage from "./page";

vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: React.ComponentProps<"a">) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
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
      { name: "tool_beta", title: "Beta", description: "Does the beta thing." },
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

beforeEach(() => {
  vi.spyOn(accountApi, "mcpConnections").mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const notes = {
  checked: "2026-10-13",
  clients: [
    {
      id: "hosted",
      name: "Hosted Chat",
      auth: "sign-in" as const,
      status: "documented" as const,
      steps: ["Add a connector with <server address>.", "Sign in when asked."],
      note: "Takes no key.",
      docs: "https://hosted.example/docs",
    },
    {
      id: "editor",
      name: "Some Editor",
      auth: "sign-in-or-key" as const,
      status: "unverified" as const,
      steps: ["Set url to <server address>."],
      docs: "https://editor.example/docs",
    },
  ],
};

describe("Connect", () => {
  test("shows a loading state while connection details load", () => {
    vi.spyOn(accountApi, "discovery").mockReturnValue(new Promise(() => {}));
    renderPage(member);
    expect(screen.getByRole("status")).toHaveTextContent(
      /loading connection details/i,
    );
    expect(screen.queryByText("AI agents (MCP)")).toBeNull();
  });

  test("asks a signed-out visitor to sign in and does not load details", () => {
    const load = vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
    renderPage(signedOut);
    expect(screen.getByText("Sign in to connect your apps")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute(
      "href",
      "/sign-in/?return_to=%2Fconnect%2F",
    );
    expect(screen.queryByRole("link", { name: "Create a key" })).toBeNull();
    void load;
  });

  test("offers a retry when details fail to load", async () => {
    vi.spyOn(accountApi, "discovery").mockRejectedValue(new Error("down"));
    renderPage(member);
    expect(
      await screen.findByText("The gateway couldn't load connection details."),
    ).toBeTruthy();
  });

  test("shows a member the addresses, the MCP server and a key link", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
    renderPage(member);
    expect(
      await screen.findByLabelText("OpenAI-compatible address"),
    ).toHaveValue("https://gw.example/openai/v1");
    expect(screen.getByLabelText("Anthropic-compatible address")).toHaveValue(
      "https://gw.example/anthropic",
    );
    expect(screen.getByLabelText("Ollama-compatible address")).toHaveValue(
      "https://gw.example/ollama",
    );
    expect(screen.getByLabelText("MCP server address")).toHaveValue(
      "https://gw.example/mcp",
    );
    expect(screen.getByRole("link", { name: "Create a key" })).toHaveAttribute(
      "href",
      "/apps?action=create-key",
    );
    // The snippet carries the address from discovery and a placeholder, never a key.
    const json = screen.getByText(/"mcpServers"/);
    expect(json.textContent).toContain("https://gw.example/mcp");
    expect(json.textContent).toContain("<your app key>");
    expect(screen.queryByText("Gateway automation")).toBeNull();
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

  test("lists the MCP tools the server reports", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue(discovery);
    renderPage(member);
    const list = await screen.findByRole("list", { name: "MCP tools" });
    const names = within(list)
      .getAllByRole("listitem")
      .map((item) => item.querySelector("code")?.textContent);
    expect(names).toEqual(["tool_alpha", "tool_beta"]);
    expect(within(list).getByText("Does the beta thing.")).toBeTruthy();
  });

  test("takes the tool list from the server, not from the page", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue({
      ...discovery,
      mcp: {
        ...discovery.mcp!,
        tools: [
          { name: "only_one", title: "One", description: "The only tool." },
        ],
      },
    });
    renderPage(member);
    const list = await screen.findByRole("list", { name: "MCP tools" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(within(list).getByText("only_one")).toBeTruthy();
    // A page that hard-coded gateway tool names would still show them here.
    for (const name of ["capabilities", "run", "job"])
      expect(within(list).queryByText(name)).toBeNull();
  });

  test("says so when the server lists no tools", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue({
      recipes: discovery.recipes,
    });
    renderPage(member);
    expect(
      await screen.findByText("This gateway didn't list any MCP tools."),
    ).toBeTruthy();
  });

  test("tells people to paste the address and sign in, not to use a key, when sign-in works", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue({
      ...discovery,
      mcp: { ...discovery.mcp!, sign_in: true },
    });
    renderPage(member);
    const steps = await screen.findByRole("list", { name: "How to connect" });
    expect(steps.textContent).toMatch(/paste this address/);
    expect(steps.textContent).toMatch(/sign in to .* and choose Allow/);
    expect(steps.textContent).not.toMatch(/key/i);
    expect(
      screen.getByText(/ChatGPT and Claude.ai sign in instead/),
    ).toBeTruthy();
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
    expect(screen.queryByRole("list", { name: "How to connect" })).toBeNull();
  });

  test("renders each client's notes from data, with the address filled in", async () => {
    vi.spyOn(accountApi, "discovery").mockResolvedValue({
      ...discovery,
      mcp: { ...discovery.mcp!, sign_in: true, clients: notes },
    });
    renderPage(member);
    expect(await screen.findByText(/checked 2026-10-13/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Hosted Chat/ }));
    expect(
      await screen.findByText("Add a connector with https://gw.example/mcp."),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "Hosted Chat documentation" }),
    ).toHaveAttribute("href", "https://hosted.example/docs");
    expect(
      screen.getByRole("button", { name: /Some Editor/ }).textContent,
    ).toMatch(/Partly unverified/);
  });

  test("lists connections and disconnects one", async () => {
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
    const revoke = vi
      .spyOn(accountApi, "revokeMcpConnection")
      .mockResolvedValue({});
    renderPage(member);
    const list = await screen.findByRole("list", { name: "Connections" });
    expect(within(list).getByText("Hosted Chat")).toBeTruthy();
    expect(within(list).getByText(/Returns to hosted.example/)).toBeTruthy();
    fireEvent.click(
      within(list).getByRole("button", { name: "Disconnect Hosted Chat" }),
    );
    await waitFor(() => expect(revoke).toHaveBeenCalledWith("mcg_1"));
  });
});
