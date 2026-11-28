import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { adminFetch, type RouteTargetChoice } from "@/lib/api";
import { RouteOrderEditor } from "./route-order-editor";
vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(), adminFetch: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const plan = { service_id: "plan", model_id: "a", name: "Plan", upstream_model: "a", billing_class: "plan" as const, available: true };
const paid = { service_id: "paid", model_id: "b", name: "Paid", upstream_model: "b", billing_class: "paid" as const, available: true };
it("selects a target, moves paid first with the keyboard and saves the backend order", async () => {
  let routes: RouteTargetChoice[] = [plan];
  vi.mocked(adminFetch).mockImplementation(async (_path, init) => {
    if (init?.method === "PUT") routes = [paid, plan];
    return { route_editor: { generation: 3, service_id: "plan", model_id: "a", routes, targets: [plan, paid] } };
  });
  render(<RouteOrderEditor serviceId="plan" modelId="a" />);
  fireEvent.click(screen.getByRole("button", { name: "Route order for a" }));
  await screen.findByLabelText("Add route for a");
  expect(screen.getByRole("button", { name: "Remove Plan" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Add route for a"), { target: { value: "paid\nb" } });
  expect(screen.queryByRole("option", { name: /Paid/ })).not.toBeInTheDocument();
  screen.getByRole("button", { name: "Move Paid up" }).focus();
  await userEvent.keyboard("{Enter}");
  fireEvent.click(screen.getByRole("button", { name: "Save routes" }));
  await screen.findByText("Routes saved");
  expect(vi.mocked(adminFetch).mock.calls.find(([, init]) => init?.method === "PUT")?.[1]).toMatchObject({
    headers: { "If-Match": '"os-3"' }, body: { targets: [ { service_id: "paid", model_id: "b" }, { service_id: "plan", model_id: "a" } ] },
  });
  expect(screen.getByText(/Paid routes may charge/)).toBeInTheDocument();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
});
it("keeps the draft after a conflict and reloads the current saved order", async () => {
  vi.mocked(adminFetch).mockImplementation(async (_path, init) => {
    if (init?.method === "PUT") throw new Error("Registry changed");
    return { route_editor: { generation: 3, service_id: "plan", model_id: "a", routes: [plan, paid], targets: [plan, paid] } };
  });
  render(<RouteOrderEditor serviceId="plan" modelId="a" />);
  fireEvent.click(screen.getByRole("button", { name: "Route order for a" }));
  fireEvent.click(await screen.findByRole("button", { name: "Move Paid up" }));
  fireEvent.click(screen.getByRole("button", { name: "Save routes" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "Move Paid up" })).toBeDisabled();
  await waitFor(() => expect(adminFetch).toHaveBeenCalledTimes(3));
});
