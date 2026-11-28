import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
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

async function acceptWith(promptMemory: Response | Promise<Response>) {
  window.history.replaceState(null, "", "/invite/#token=invite-token");
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status });
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === "/auth/api/options")
      return json({ built_in: true, oidc: false });
    if (path === "/auth/api/invite") return json(invitation);
    if (path === "/auth/api/invite/accept")
      return json({ next: "/", credential: { api_key: "opk_test" } });
    if (path === "/me/api/prompt-memory") return promptMemory;
    return json({ error: { code: "not_found", message: "Not found" } }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const view = render(
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
  return { client, fetchMock, unmount: view.unmount };
}

describe("Invite: the welcome card's line on prompts", () => {
  it("says prompts are kept, private, and where to change it", async () => {
    await acceptWith(
      new Response(JSON.stringify(memory(true, false)), { status: 200 }),
    );
    expect(
      await screen.findByText(
        "Azizam can keep what you type so you can find it again. Only you can see it. Change this in Your settings.",
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

  it("claims nothing after prompt settings reach a terminal read error", async () => {
    let finish!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => {
      finish = resolve;
    });
    const fixture = await acceptWith(pending);
    try {
      const reads = () =>
        fixture.fetchMock.mock.calls.filter(
          ([input]) => String(input) === "/me/api/prompt-memory",
        );
      await waitFor(() => expect(reads()).toHaveLength(1));
      expect(screen.queryByText(/what you type/)).toBeNull();
      await act(async () => {
        finish(
          Response.json(
            { error: { code: "boom", message: "Down" } },
            { status: 500 },
          ),
        );
        await pending;
      });
      await waitFor(() =>
        expect(
          fixture.client.getQueryState(["account", "prompt-memory"]),
        ).toMatchObject({ status: "error", fetchStatus: "idle" }),
      );
      expect(reads()).toHaveLength(1);
      expect(
        screen.getByRole("heading", { name: "Your account is ready" }),
      ).toBeVisible();
      expect(screen.queryByText(/what you type/)).toBeNull();
    } finally {
      fixture.unmount();
      fixture.client.clear();
    }
  });
});
