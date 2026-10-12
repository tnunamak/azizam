import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

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
      { name: "tool_alpha", title: "Alpha", description: "Does the alpha thing." },
      { name: "tool_beta", title: "Beta", description: "Does the beta thing." },
    ],
  },
};

function renderPage(viewer: Viewer) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ViewerContext.Provider value={viewer}>
        <ConnectPage />
      </ViewerContext.Provider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Connect", () => {
  test("shows a loading state while connection details load", () => {
    vi.spyOn(accountApi, "discovery").mockReturnValue(new Promise(() => {}));
    renderPage(member);
    expect(screen.getByRole("status")).toHaveTextContent(/loading connection details/i);
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
    expect(await screen.findByLabelText("OpenAI-compatible address")).toHaveValue(
      "https://gw.example/openai/v1",
    );
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
        tools: [{ name: "only_one", title: "One", description: "The only tool." }],
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
});
