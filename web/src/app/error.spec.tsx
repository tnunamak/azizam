import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { axe } from "jest-axe";
import RouteError from "./error";
import GlobalError from "./global-error";

const SECRET_MESSAGE = "TypeError: Cannot read properties of undefined (reading 'opk_live_secret_1234')";
const STACK = "TypeError: boom\n    at PeoplePage (/_next/static/chunks/app/people/page-0123.js:1:2)";

function crash() {
  const error = Object.assign(new Error(SECRET_MESSAGE), { digest: "digest-9f3a" });
  error.stack = STACK;
  return error;
}

let consoleError: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(cleanup);

describe("route error boundary (app/error.tsx)", () => {
  it("renders a calm crashed state with Try again and Go home", () => {
    render(<RouteError error={crash()} reset={() => {}} />);
    expect(screen.getByRole("heading", { level: 1, name: "This page stopped working" })).toBeVisible();
    expect(screen.getByText("Something went wrong on this page")).toBeVisible();
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Go home" })).toHaveAttribute("href", "/");
  });

  it("calls reset when the person chooses Try again, once per click", async () => {
    const reset = vi.fn();
    render(<RouteError error={crash()} reset={reset} />);
    expect(reset).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(reset).toHaveBeenCalledTimes(1);
  });

  it("never shows the error text, the stack or the digest to a member", () => {
    const { container } = render(<RouteError error={crash()} reset={() => {}} />);
    const text = container.textContent ?? "";
    for (const leak of [SECRET_MESSAGE, "TypeError", "opk_live", "at PeoplePage", "_next/static", "digest-9f3a", "Cannot read"])
      expect(text).not.toContain(leak);
    expect(container.innerHTML).not.toContain("digest-9f3a");
    expect(within(container).queryByText(/technical details/i)).toBeNull();
  });

  it("logs only a fixed label and the digest, never the error, its message or its stack", () => {
    const error = crash();
    const { rerender } = render(<RouteError error={error} reset={() => {}} />);
    expect(consoleError).toHaveBeenCalledTimes(1);
    expect(consoleError).toHaveBeenCalledWith("Azizam page error", "digest-9f3a");
    expect(JSON.stringify(consoleError.mock.calls)).not.toMatch(/TypeError|opk_live|PeoplePage|_next\/static|Cannot read/);
    expect(consoleError.mock.calls[0]).not.toContain(error);
    rerender(<RouteError error={error} reset={() => {}} />);
    expect(consoleError).toHaveBeenCalledTimes(1);
  });

  it("logs a placeholder when the error has no digest", () => {
    const error = new Error(SECRET_MESSAGE);
    render(<RouteError error={error} reset={() => {}} />);
    expect(consoleError).toHaveBeenCalledWith("Azizam page error", "no digest");
  });

  it("says what to do in plain words", () => {
    render(<RouteError error={crash()} reset={() => {}} />);
    expect(
      screen.getByText("Try again. If it keeps happening, go Home and tell the person who runs this gateway."),
    ).toBeVisible();
  });

  it("has no serious accessibility violations", async () => {
    const { container } = render(<RouteError error={crash()} reset={() => {}} />);
    const result = await axe(container);
    expect(result.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? ""))).toEqual([]);
  });
});

describe("global error boundary (app/global-error.tsx)", () => {
  // It owns the whole document, so it is rendered to a string; React cannot mount <html> into a div.
  const markup = (error = crash()) => renderToStaticMarkup(<GlobalError error={error} reset={() => {}} />);

  it("renders its own html and body, with no provider or shell", () => {
    const html = markup();
    expect(html.startsWith("<html")).toBe(true);
    expect(html).toContain('<html lang="en">');
    expect(html).toContain("<body>");
    expect(html).toContain("<title>Azizam</title>");
    expect(html).toContain("Something went wrong");
    expect(html).toContain("Try again");
    expect(html).toContain('<a class="home" href="/">Go home</a>');
    expect(html).not.toContain("data-slot");
  });

  it("styles itself inline, with a light and a dark scheme and no stylesheet link", () => {
    const html = markup();
    expect(html).toContain("<style>");
    expect(html).toContain("prefers-color-scheme: dark");
    expect(html).not.toContain("<link");
    expect(html).not.toContain("<script");
  });

  it("never shows the error text, the stack or the digest", () => {
    const html = markup();
    for (const leak of [SECRET_MESSAGE, "TypeError", "opk_live", "at PeoplePage", "_next/static", "digest-9f3a"])
      expect(html).not.toContain(leak);
  });

  it("calls reset when Try again is pressed and logs only a label and the digest", async () => {
    const reset = vi.fn();
    const error = crash();
    // A document cannot be mounted into a div; mount the same tree in the test document instead.
    const host = document.createElement("div");
    document.body.append(host);
    const { createRoot } = await import("react-dom/client");
    const { act } = await import("react");
    const root = createRoot(host);
    await act(async () => {
      root.render(
        <GlobalError error={error} reset={reset} />,
      );
    });
    expect(consoleError).toHaveBeenCalledWith("Azizam root error", "digest-9f3a");
    expect(consoleError.mock.calls.flat()).not.toContain(error);
    expect(JSON.stringify(consoleError.mock.calls)).not.toMatch(/TypeError|opk_live|PeoplePage|Cannot read/);
    const button = host.querySelector("button") ?? document.querySelector("button");
    expect(button).not.toBeNull();
    await userEvent.click(button!);
    expect(reset).toHaveBeenCalledTimes(1);
    await act(async () => root.unmount());
    host.remove();
  });
});
