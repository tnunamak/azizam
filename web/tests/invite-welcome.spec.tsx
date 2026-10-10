import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import InvitePage from "@/app/invite/page";

// The new person's welcome card says in one line whether Azizam keeps what
// they type (prompt-memory spec, "First run or onboarding").

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  window.history.replaceState(null, "", "/");
  vi.unstubAllGlobals();
});

const invitation = {
  plan: {
    id: "plan-1",
    name: "Family",
    description: null,
    profile: { allow: { intents: ["chat"] } },
  },
  email: "robin@example.test",
  expires_at: Math.floor(Date.now() / 1000) + 86_400,
  owner_name: "Sam",
  instance_name: "Our gateway",
};

function memory(remember: boolean, neverKeep: boolean) {
  return {
    prompt_memory: {
      remember,
      retention: "never",
      effective_remember: remember && !neverKeep,
      effective_retention: "never",
      household: {
        default_remember: true,
        never_keep: neverKeep,
        retention_cap: "never",
      },
    },
  };
}

async function acceptWith(promptMemory: Response) {
  window.history.replaceState(null, "", "/ui/invite/#token=invite-token");
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/auth/api/options")
        return json({ built_in: true, oidc: false });
      if (path === "/auth/api/invite") return json(invitation);
      if (path === "/auth/api/invite/accept")
        return json({ next: "/ui/", credential: { api_key: "opk_test" } });
      if (path === "/me/api/prompt-memory") return promptMemory;
      return json({ error: { code: "not_found", message: "Not found" } }, 404);
    }),
  );
  const user = userEvent.setup();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <InvitePage />
    </QueryClientProvider>,
  );
  await user.type(await screen.findByLabelText("Your name"), "Robin");
  await user.type(screen.getByLabelText("Password"), "long-enough-password");
  await user.type(
    screen.getByLabelText("Confirm password"),
    "long-enough-password",
  );
  await user.click(screen.getByRole("button", { name: "Create account" }));
  await screen.findByRole("heading", { name: "Your account is ready" });
}

describe("Invite: the welcome card's line on prompts", () => {
  it("says prompts are remembered, private, and where to change it", async () => {
    await acceptWith(
      new Response(JSON.stringify(memory(true, false)), { status: 200 }),
    );
    expect(
      await screen.findByText(
        "Azizam can remember what you type so you can find things again. Only you can see saved prompts. Change this in Your settings.",
      ),
    ).toBeVisible();
  });

  it("says nothing is kept when the household turned prompts off", async () => {
    await acceptWith(
      new Response(JSON.stringify(memory(true, true)), { status: 200 }),
    );
    expect(
      await screen.findByText("This gateway doesn’t keep what you type."),
    ).toBeVisible();
    expect(screen.queryByText(/remembers what you type/)).toBeNull();
  });

  it("claims nothing when the setting can't be read", async () => {
    await acceptWith(
      new Response(
        JSON.stringify({ error: { code: "boom", message: "Down" } }),
        { status: 500 },
      ),
    );
    // Let the failed read settle before checking the card stayed silent.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByText(/what you type/)).toBeNull();
  });
});
