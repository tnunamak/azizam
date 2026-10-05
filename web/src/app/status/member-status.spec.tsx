import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemberStatus } from "./status-view";
import { productApi, type MemberStatus as Snapshot } from "@/lib/product-api";
import { ApiError } from "@/lib/api";

vi.mock("./status-client", () => ({ StatusClient: () => <p>Owner status</p> }));
vi.mock("next/link", () => ({
  default: ({ href, children, ...props }: React.ComponentProps<"a">) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));
const snapshot: Snapshot = {
  scope: "member",
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
    vi.spyOn(productApi, "status").mockResolvedValue(snapshot);
    const user = userEvent.setup();
    mount();
    expect(
      await screen.findByRole("heading", { name: "Images" }),
    ).toBeVisible();
    expect(screen.getByText("Flux", { exact: true })).toBeVisible();
    expect(
      screen.queryByText("image-model", { exact: true }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("Ask the owner", { exact: true }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Not measured yet")).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "View receipt my-failed-request" }),
    ).toHaveAttribute("href", snapshot.receipts[0].href);
    await user.click(screen.getByText("Details", { exact: true }));
    expect(
      screen.getByText("Names you can use", { exact: true }),
    ).toBeVisible();
    expect(screen.getByText("image, flux-dev", { exact: true })).toBeVisible();
    expect(screen.queryByText("image, flux-dev, image")).toBeNull();
    expect(screen.queryByText("Not measured yet")).not.toBeInTheDocument();
    expect(
      screen.queryByText(/GPU|VRAM|Raw probe data/),
    ).not.toBeInTheDocument();
  });
  it("uses the music and sound effects icons from the contract and the honest aggregate state", async () => {
    vi.spyOn(productApi, "status").mockResolvedValue({
      ...snapshot,
      status: {
        state: "unavailable",
        reason_code: "unavailable",
        affected_capability_ids: ["image"],
        counts: { total: 1, available: 0, affected: 1 },
      },
      capabilities: [
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
    expect(await screen.findByRole("img", { name: "Music" })).toHaveAttribute(
      "data-icon-key",
      "music",
    );
    expect(screen.getByRole("img", { name: "Sound effects" })).toHaveAttribute(
      "data-icon-key",
      "sfx",
    );
    expect(
      screen.getByRole("img", { name: "Music" }).querySelector("svg"),
    ).toHaveClass("lucide-music-2");
    expect(
      screen.getByRole("img", { name: "Sound effects" }).querySelector("svg"),
    ).toHaveClass("lucide-waves");
    expect(
      screen.getByText("Images are unavailable."),
    ).toBeVisible();
    expect(screen.getByText("Unavailable", { exact: true })).toBeVisible();
  });
  it("teaches an account with no capabilities or receipts how to get access", async () => {
    vi.spyOn(productApi, "status").mockResolvedValue({
      ...snapshot,
      capabilities: [],
      receipts: [],
    });
    mount();
    expect(
      await screen.findByText("No models are available to your account yet"),
    ).toBeVisible();
    expect(screen.getByText(/No receipts for your account yet/)).toBeVisible();
  });
  it("shows neutral presentation when an older snapshot has no presentation fields", async () => {
    vi.spyOn(productApi, "status").mockResolvedValue({
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
    expect(
      await screen.findByRole("heading", { name: "Capability" }),
    ).toBeVisible();
    expect(screen.getByText("Model details unavailable")).toBeVisible();
    expect(
      screen.queryByText("raw-workflow", { exact: true }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "Capability" }).querySelector("svg"),
    ).toHaveClass("lucide-circle-question-mark");
  });
  it("hides the names heading when the contract has no names", async () => {
    vi.spyOn(productApi, "status").mockResolvedValue({
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
    expect(
      await screen.findByRole("heading", { name: "Images" }),
    ).toBeVisible();
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
