import { afterEach, expect, it } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { usePageOutage } from "./page-outage";

afterEach(cleanup);

function Source({ id, fail }: { id: string; fail: () => unknown }) {
  useQuery({
    queryKey: ["outage-test", id],
    meta: { page: "demo" },
    retry: false,
    queryFn: async () => {
      const failure = fail();
      if (failure) throw failure;
      return id;
    },
  });
  return null;
}
function Probe() {
  const { count, outage, retryAll } = usePageOutage("demo");
  return (
    <button type="button" onClick={retryAll}>
      {`count ${count} outage ${outage}`}
    </button>
  );
}
function mount(sources: Array<() => unknown>) {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <Probe />
      {sources.map((fail, index) => (
        <Source key={index} id={String(index)} fail={fail} />
      ))}
    </QueryClientProvider>,
  );
}

it("is an outage when two sources fail with a 503", async () => {
  mount([() => new ApiError("x", "", 503), () => new TypeError("offline"), () => null]);
  expect(await screen.findByRole("button", { name: "count 2 outage true" })).toBeVisible();
});

it("is not an outage for one 503 or for permission failures", async () => {
  mount([() => new ApiError("x", "", 503), () => new ApiError("x", "", 403), () => new ApiError("x", "", 404)]);
  expect(await screen.findByRole("button", { name: "count 1 outage false" })).toBeVisible();
});

it("asks the failed sources again with one retry", async () => {
  let down = true;
  mount([() => down && new ApiError("x", "", 503), () => down && new ApiError("x", "", 503)]);
  const button = await screen.findByRole("button", { name: "count 2 outage true" });
  down = false;
  await userEvent.click(button);
  await waitFor(() => expect(screen.getByRole("button", { name: "count 0 outage false" })).toBeVisible());
});
