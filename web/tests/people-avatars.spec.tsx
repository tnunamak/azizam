import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import PeoplePage from "@/app/people/page";
import { getGeneratedAvatar } from "@/components/product/person-avatar";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const person = (id: string, name: string) => ({
  id, name, email: `${id}@example.test`, plan_id: null, plan_name: null,
  disabled: false, protected: false, builtin: false, role: "member", last_active: null,
  requests_this_month: null, grants: [],
});

function stub(names: Record<string, string>, avatars: Record<string, { choice: string; picture_url: string | null }>) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/me/api/profile/avatars")) return Response.json({ people: avatars });
    return Response.json(
      url.endsWith("/people")
        ? { people: Object.entries(names).map(([id, name]) => person(id, name)), access_plans: [] }
        : url.endsWith("/invites") ? { invites: [], access_plans: [], invite_access_plan: null } : { access_plans: [] },
    );
  }));
}

async function rowFills(names: Record<string, string>, avatars = {}) {
  stub(names, avatars);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><PeoplePage /></QueryClientProvider>);
  const result: Record<string, string> = {};
  for (const [id, name] of Object.entries(names)) {
    const row = (await screen.findByText(name)).closest("[data-slot='list-row']") as HTMLElement;
    const disc = row.querySelector<HTMLElement>("[data-slot='person-mark'] > span")!;
    await waitFor(() => expect(disc.style.backgroundImage).not.toBe(""));
    result[id] = disc.style.backgroundImage;
  }
  view.unmount();
  client.clear();
  return result;
}

it("draws a person's mark from their id, so a rename keeps the colour", async () => {
  const before = await rowFills({ usr_robin: "Robin", usr_zoe: "Zoë" });
  const after = await rowFills({ usr_robin: "Robin Quill-Adeyemi", usr_zoe: "Zoë" });
  expect(after.usr_robin).toBe(before.usr_robin);
  expect(before.usr_robin).toBe(getGeneratedAvatar("usr_robin").background);
  expect(before.usr_zoe).toBe(getGeneratedAvatar("usr_zoe").background);
});

it("shows the avatar each person chose", async () => {
  const fills = await rowFills(
    { usr_robin: "Robin", usr_zoe: "Zoë" },
    { usr_zoe: { choice: "v7", picture_url: null } },
  );
  expect(fills.usr_zoe).toBe(getGeneratedAvatar("usr_zoe", "v7").background);
  expect(fills.usr_robin).toBe(getGeneratedAvatar("usr_robin").background);
});
