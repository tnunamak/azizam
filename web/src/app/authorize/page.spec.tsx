import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { accountApi, type McpConsentRequest } from "@/lib/account-api";
import { ApiError } from "@/lib/api";
import AuthorizePage, { navigate } from "./page";

const request: McpConsentRequest = {
  client: {
    name: "Doc Client",
    client_id: "https://client.example/oauth/client.json",
    verified_domain: "client.example",
    registration: "metadata_document",
    redirect_host: "client.example",
  },
  person: { name: "Ann", email: "ann@example.org" },
  plan: { name: "Household" },
  can_connect: true,
  access: ["Make images within your plan"],
  lasts: "Until you disconnect it on the Connect page",
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AuthorizePage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  window.history.replaceState(null, "", "/authorize/?request=req_1");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Authorize (MCP consent)", () => {
  test("shows who is asking, as whom, with what plan, and focuses Cancel", async () => {
    const load = vi
      .spyOn(accountApi, "mcpConsentRequest")
      .mockResolvedValue(request);
    renderPage();
    expect(
      await screen.findByRole("heading", { name: /Connect Doc Client to/ }),
    ).toBeTruthy();
    expect(load).toHaveBeenCalledWith("req_1");
    expect(
      screen.getByText(/signed in as Ann \(ann@example.org\)/),
    ).toBeTruthy();
    expect(screen.getByText("Household")).toBeTruthy();
    expect(screen.getByText("Make images within your plan")).toBeTruthy();
    expect(
      screen.getByText("client.example", { selector: "strong" }),
    ).toBeTruthy();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Cancel" }),
    );
  });

  test("warns when the app registered itself", async () => {
    vi.spyOn(accountApi, "mcpConsentRequest").mockResolvedValue({
      ...request,
      client: {
        ...request.client,
        verified_domain: null,
        registration: "self_registered",
      },
    });
    renderPage();
    expect(await screen.findByText(/can't confirm who made it/)).toBeTruthy();
  });

  test("Allow sends the decision and follows the gateway's redirect", async () => {
    vi.spyOn(accountApi, "mcpConsentRequest").mockResolvedValue(request);
    const decide = vi
      .spyOn(accountApi, "decideMcpConsent")
      .mockResolvedValue({
        redirect: "https://client.example/cb?code=c&state=s&iss=x",
      });
    const go = vi.spyOn(navigate, "to").mockImplementation(() => {});
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Allow" }));
    await waitFor(() =>
      expect(go).toHaveBeenCalledWith(
        "https://client.example/cb?code=c&state=s&iss=x",
      ),
    );
    expect(decide).toHaveBeenCalledWith("req_1", "allow");
  });

  test("Cancel sends a denial back to the app", async () => {
    vi.spyOn(accountApi, "mcpConsentRequest").mockResolvedValue(request);
    const decide = vi
      .spyOn(accountApi, "decideMcpConsent")
      .mockResolvedValue({
        redirect: "https://client.example/cb?error=access_denied",
      });
    const go = vi.spyOn(navigate, "to").mockImplementation(() => {});
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(go).toHaveBeenCalledWith(
        "https://client.example/cb?error=access_denied",
      ),
    );
    expect(decide).toHaveBeenCalledWith("req_1", "deny");
  });

  test("a person without access cannot allow", async () => {
    vi.spyOn(accountApi, "mcpConsentRequest").mockResolvedValue({
      ...request,
      can_connect: false,
    });
    renderPage();
    expect(
      await screen.findByText("Your account can't use AI here yet"),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Allow" })).toBeDisabled();
  });

  test("an ended request says to start again from the app", async () => {
    vi.spyOn(accountApi, "mcpConsentRequest").mockRejectedValue(
      new ApiError("request_expired", "gone", 410),
    );
    renderPage();
    expect(await screen.findByText("This request has ended")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Allow" })).toBeNull();
  });

  test("without a request id there is nothing to approve", async () => {
    window.history.replaceState(null, "", "/authorize/");
    renderPage();
    expect(await screen.findByText("Nothing to approve")).toBeTruthy();
  });
});
