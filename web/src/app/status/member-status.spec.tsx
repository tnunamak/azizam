import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemberStatus } from "./status-view";
import { productApi, type MemberStatus as Snapshot } from "@/lib/product-api";
import { ApiError } from "@/lib/api";

vi.mock("./status-client", () => ({ StatusClient: () => <p>Owner status</p> }));
// Capability states come from the shared model, which reads the Home snapshot.
const home = vi.hoisted(() => ({ rows: [] as Array<Record<string, unknown>> }));
vi.mock("@/app/home/home-api", () => ({
  homeApi: {
    home: async () => ({
      authenticated: true,
      capabilities: home.rows,
      aliases: [],
      receipts: [],
      apps: [],
    }),
  },
}));
/** Serve one snapshot from both member endpoints, as a real gateway does. */
function serve(value: Snapshot) {
  home.rows = value.capabilities.map((row) => ({
    ...row,
    availability: row.measured
      ? { evidence: { source: "status", observed_at_s: Date.now() / 1000 } }
      : null,
  }));
  return vi.spyOn(productApi, "status").mockResolvedValue(value);
}
vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: React.ComponentProps<"a">) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));
const snapshot: Snapshot = {
  scope: "member",
  gateway: { owner_name: null },
  status: {
    state: "unknown",
    reason_code: "unknown",
    affected_capability_ids: ["image"],
    counts: { total: 1, available: 0, affected: 1 },
  },
  capabilities: [
    {
      capability: "image",
      capability_title: "Images",
      icon_key: "image",
      title: "Flux",
      recommended_model: "Flux",
      use_names: ["image", "flux-dev"],
      use_name: "image",
      summary: "Generate images with Flux.",
      intent: "image.generate",
      model: "image-model",
      workflow: null,
      status: "unknown",
      available: false,
      measured: false,
      observed_at_s: null,
      reason_code: "unknown",
      availability: { evidence: {} },
    },
  ],
  receipts: [
    {
      request_id: "my-failed-request",
      status: "failed",
      intent: "image.generate",
      created_at: null,
      href: "/ui/usage/?receipt=my-failed-request",
    },
  ],
  help: "Ask the owner if an allowed capability is unavailable.",
};
function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemberStatus />
    </QueryClientProvider>,
  );
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("member Status diagnosis", () => {
  it("shows server presentation and keeps raw names and unknown metrics in details", async () => {
    serve(snapshot);
    const user = userEvent.setup();
    mount();
    expect(await screen.findByText("Images", { selector: "p" })).toBeVisible();
    // The recommended model sits with the names, one tap away (word budget).
    expect(
      screen.queryByText("image-model", { exact: true }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("Ask the owner", { exact: true }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Not measured yet")).not.toBeInTheDocument();
    const receipt = screen.getByRole("link", { name: "View request receipt" });
    expect(receipt).toHaveAttribute("href", snapshot.receipts[0].href);
    // The visible capability and state describe the link; the id stays out.
    expect(receipt).toHaveAccessibleDescription(/Images.*Failed/);
    expect(receipt).not.toHaveAccessibleDescription(/my-failed-request/);
    await user.click(screen.getByText("Model names", { exact: true }));
    expect(screen.getByText("Flux", { exact: true })).toBeVisible();
    expect(screen.getByText(/Names you can use/)).toBeInTheDocument();
    expect(screen.getByText("image, flux-dev", { exact: true })).toBeVisible();
    expect(screen.queryByText("image, flux-dev, image")).toBeNull();
    expect(screen.queryByText("Not measured yet")).not.toBeInTheDocument();
    expect(
      screen.queryByText(/GPU|VRAM|Raw probe data/),
    ).not.toBeInTheDocument();
  });
  it("uses the music and sound effects icons from the contract and the honest aggregate state", async () => {
    serve({
      ...snapshot,
      status: {
        state: "unavailable",
        reason_code: "unavailable",
        affected_capability_ids: ["image"],
        counts: { total: 3, available: 2, affected: 1 },
      },
      capabilities: [
        {
          ...snapshot.capabilities[0],
          status: "unavailable",
          reason_code: "unavailable",
          measured: true,
        },
        {
          ...snapshot.capabilities[0],
          capability: "music",
          capability_title: "Music",
          icon_key: "music",
          recommended_model: "ACE-Step XL Turbo",
          status: "ready",
          measured: true,
          available: true,
        },
        {
          ...snapshot.capabilities[0],
          capability: "sfx",
          capability_title: "Sound effects",
          icon_key: "sfx",
          recommended_model: "Stable Audio",
          status: "ready",
          measured: true,
          available: true,
        },
      ],
    });
    mount();
    const music = (await screen.findByText("Music", { selector: "p" })).closest(
      "li",
    )!;
    expect(music.querySelector("svg")).toHaveClass("lucide-music-2");
    const sfx = screen
      .getByText("Sound effects", { selector: "p" })
      .closest("li")!;
    expect(sfx.querySelector("svg")).toHaveClass("lucide-waves");
    expect(await screen.findByRole("status")).toHaveTextContent("Images down");
    expect(screen.queryByText("Everything works")).toBeNull();
    expect(screen.getByText("Down", { exact: true })).toBeVisible();
  });
  it("teaches an account with no capabilities or receipts how to get access", async () => {
    serve({
      ...snapshot,
      capabilities: [],
      receipts: [],
    });
    mount();
    expect(
      await screen.findByText("No models are available to your account yet"),
    ).toBeVisible();
    expect(screen.getByText("No requests yet.")).toBeVisible();
  });
  it("shows neutral presentation when an older snapshot has no presentation fields", async () => {
    serve({
      ...snapshot,
      capabilities: [
        {
          capability: "music",
          intent: "audio.generate",
          model: "raw-workflow",
          workflow: null,
          status: "unknown",
          available: false,
          measured: false,
          reason_code: "unknown",
        },
      ],
    });
    mount();
    // No presentation fields: the state stays Unknown, never Ready.
    const row = (await screen.findByText("Music", { selector: "p" })).closest(
      "li",
    )!;
    expect(row).toHaveTextContent("Unknown");
    expect(row).not.toHaveTextContent("Ready");
    await userEvent.click(screen.getByText("Model names", { exact: true }));
    expect(screen.getByText("Model details unavailable")).toBeVisible();
    expect(
      screen.queryByText("raw-workflow", { exact: true }),
    ).not.toBeInTheDocument();
  });
  it("hides the names heading when the contract has no names", async () => {
    serve({
      ...snapshot,
      capabilities: [
        {
          ...snapshot.capabilities[0],
          use_names: [],
          use_name: undefined,
          summary: "",
        },
      ],
    });
    mount();
    expect(await screen.findByText("Images", { selector: "p" })).toBeVisible();
    expect(
      screen.queryByText("Names you can use", { exact: true }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("Names unavailable", { exact: true }),
    ).not.toBeInTheDocument();
  });
  it("offers sign-in after a 401 without retrying an expired session", async () => {
    const status = vi
      .spyOn(productApi, "status")
      .mockRejectedValue(new ApiError("unauthorized", "Expired", 401));
    mount();
    expect(
      await screen.findByRole("link", { name: "Sign in" }),
    ).toHaveAttribute("href", "/sign-in/?return_to=%2Fui%2Fstatus%2F");
    expect(status).toHaveBeenCalledOnce();
  });
  it("renders the loading state until the scoped request resolves", async () => {
    vi.spyOn(productApi, "status").mockReturnValue(new Promise(() => {}));
    mount();
    expect(
      screen.getByRole("status", { name: "Loading your model status" }),
    ).toHaveAttribute("aria-busy", "true");
  });
});

describe("member Status leaves app plumbing out", () => {
  it("never says 'Embeddings down' or lists decisions to a member", async () => {
    const ready = snapshot.capabilities[0];
    serve({
      ...snapshot,
      receipts: [],
      capabilities: [
        { ...ready, status: "ready", available: true, measured: true, reason_code: "ready" },
        {
          ...ready,
          capability: "embeddings",
          capability_title: "Embeddings",
          icon_key: "embed",
          intent: "text.embeddings",
          status: "unavailable",
          available: false,
          measured: true,
          reason_code: "engine_down",
        },
        {
          ...ready,
          capability: "decisions",
          capability_title: "Decisions",
          icon_key: "decide",
          intent: "text.decisions",
          status: "ready",
          available: true,
          measured: true,
          reason_code: "ready",
        },
      ],
    });
    mount();
    expect(
      await screen.findByRole("heading", { name: "What you can use" }),
    ).toBeVisible();
    expect(await screen.findByText("Images", { selector: "p" })).toBeVisible();
    expect(screen.getByRole("status")).not.toHaveTextContent(/Embeddings/i);
    expect(document.body.textContent).not.toMatch(
      /Embeddings|Decisions|Your capabilities/,
    );
  });
});
