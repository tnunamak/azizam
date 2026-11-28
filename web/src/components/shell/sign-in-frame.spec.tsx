import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { SignInFrame, useReportSignInBusy } from "./sign-in-frame";

/** A stand-in for the page's controls: it reports whether a submit or a redirect is running, as the sign-in page does. */
function Busy({ busy }: { busy: boolean }) {
  useReportSignInBusy(busy);
  return <h1>Sign in to the test household</h1>;
}

const footer = () => document.querySelector("footer");

it("frames the page in one main landmark, with a footer that links to the product site", () => {
  render(
    <SignInFrame themeToggle={<button type="button">Toggle theme</button>}>
      <h1>Sign in to the test household</h1>
    </SignInFrame>,
  );
  expect(screen.getByRole("main")).toHaveAttribute("id", "main-content");
  expect(screen.getByRole("heading", { name: "Sign in to the test household" })).toBeVisible();
  expect(screen.getByRole("contentinfo")).toContainElement(screen.getByRole("link", { name: "About Azizam" }));
  expect(screen.getByRole("link", { name: "About Azizam" })).toHaveAttribute("href", "https://azizam.dev/");
  expect(screen.getByRole("button", { name: "Toggle theme" })).toBeVisible();
  // The product name is said once, in the link: the footer has no second copy of it.
  expect(screen.getByRole("contentinfo").textContent?.match(/Azizam/g)).toHaveLength(1);
});

it("has no header and no navigation: the sign-in page is one column", () => {
  render(
    <SignInFrame themeToggle={null}>
      <h1>Sign in</h1>
    </SignInFrame>,
  );
  expect(screen.queryByRole("banner")).toBeNull();
  expect(screen.queryByRole("navigation")).toBeNull();
});

it("holds the footer while a control on the page is busy, and frees it when the control is idle", () => {
  const frame = (busy: boolean) => (
    <SignInFrame themeToggle={<button type="button">Toggle theme</button>}>
      <Busy busy={busy} />
    </SignInFrame>
  );
  const { rerender } = render(frame(false));
  expect(footer()).not.toHaveAttribute("inert");
  rerender(frame(true));
  expect(footer()).toHaveAttribute("inert");
  rerender(frame(false));
  expect(footer()).not.toHaveAttribute("inert");
});

it("frees the footer when the busy control leaves the page", () => {
  const { rerender } = render(
    <SignInFrame themeToggle={null}>
      <Busy busy />
    </SignInFrame>,
  );
  expect(footer()).toHaveAttribute("inert");
  rerender(
    <SignInFrame themeToggle={null}>
      <h1>Who’s signing in?</h1>
    </SignInFrame>,
  );
  expect(footer()).not.toHaveAttribute("inert");
});
