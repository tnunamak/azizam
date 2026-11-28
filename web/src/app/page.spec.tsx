import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import RootPage from "./page";

// The viewer answer the root page chooses from: Home for a signed-in person, the sign-in page for anyone else.
let mockViewer: { data?: { authenticated: boolean }; isPending: boolean; isError: boolean; isFetching: boolean; refetch: () => void };

vi.mock("@/components/shell/session", () => ({ useViewerQuery: () => mockViewer }));
vi.mock("./_home/home-page", () => ({ default: () => <h1>Home for the signed-in person</h1> }));
vi.mock("./sign-in/page", () => ({ default: () => <h1>Sign in to the test household</h1> }));

const settled = (data: { authenticated: boolean }) => ({
  data,
  isPending: false,
  isError: false,
  isFetching: false,
  refetch: () => {},
});

function mount() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <RootPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockViewer = settled({ authenticated: false });
});
afterEach(() => {
  vi.clearAllMocks();
});

it("shows Home to a signed-in person", () => {
  mockViewer = settled({ authenticated: true });
  mount();
  expect(screen.getByRole("heading", { name: "Home for the signed-in person" })).toBeVisible();
});

it("shows the sign-in page to a signed-out visitor, with no redirect", () => {
  mount();
  expect(screen.getByRole("heading", { name: "Sign in to the test household" })).toBeVisible();
});

it("shows nothing while the session is checked, so no sign-in heading flashes at a signed-in person", () => {
  mockViewer = { data: undefined, isPending: true, isError: false, isFetching: true, refetch: () => {} };
  const { container } = mount();
  expect(container).toBeEmptyDOMElement();
});

it("says the account did not load, rather than offering sign-in, when the session check fails", () => {
  mockViewer = { data: undefined, isPending: false, isError: true, isFetching: false, refetch: () => {} };
  mount();
  expect(screen.getByRole("heading", { name: "Couldn’t load your account" })).toBeVisible();
  expect(screen.queryByRole("heading", { name: "Sign in to the test household" })).toBeNull();
});
