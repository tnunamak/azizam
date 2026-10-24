import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { SignInFrame } from "./sign-in-frame";

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
