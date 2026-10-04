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
    reason: "Availability has not been measured yet.",
  },
  capabilities: [
    {
      capability: "image",
      intent: "image.generate",
      model: "image-model",
      workflow: null,
      status: "unknown",
      available: false,
      measured: false,
      observed_at_s: null,
      reason: "Availability has not been measured yet.",
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
  it("keeps unknown health distinct from a failed scoped receipt and copies a useful handoff", async () => {
    vi.spyOn(productApi, "status").mockResolvedValue(snapshot);
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText");
    mount();
    expect(await screen.findByText("Not measured yet")).toBeVisible();
    expect(
      screen.getByRole("link", { name: "View receipt my-failed-request" }),
    ).toHaveAttribute("href", snapshot.receipts[0].href);
    await user.click(screen.getByText("Ask the owner", { exact: true }));
    await user.click(
      screen.getByRole("button", { name: "Copy status summary" }),
    );
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      expect.stringContaining("Request my-failed-request: failed."),
    );
    expect(
      screen.queryByText(/GPU|VRAM|Raw probe data/),
    ).not.toBeInTheDocument();
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
