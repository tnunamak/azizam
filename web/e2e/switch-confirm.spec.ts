// The confirm before a switch or a sign-out that would lose work, and the account menu's Switch person (account
// switcher W2 and W3; SECURITY.md section 9, AC2 and AC8). Each test signs out a member from isolated.member().
import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures/isolation";

const UNSENT = "Plan a picnic for Saturday";

async function openAccountMenu(page: Page) {
  await page.getByRole("button", { name: /^Account menu:/ }).click();
}

/** Types into the Try chat prompt without running it. */
async function typeUnsentMessage(page: Page) {
  await page.goto("/try/");
  const prompt = page.getByRole("textbox", { name: "Prompt" });
  await prompt.fill(UNSENT);
  return prompt;
}

test("switch: confirm lists the unsent Try message and Keep working keeps it", async ({ isolated }) => {
  const member = await isolated.member({ name: "Keep member" });
  const prompt = await typeUnsentMessage(member.page);
  await openAccountMenu(member.page);
  await member.page.getByRole("menuitem", { name: "Switch person" }).click();

  const dialog = member.page.getByRole("dialog", { name: "Switch person?" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("listitem")).toHaveText(["Your unsent message in Try"]);
  const keep = dialog.getByRole("button", { name: "Keep working" });
  await expect(keep).toBeFocused();
  await keep.click();

  await expect(dialog).toBeHidden();
  await expect(prompt).toHaveValue(UNSENT);
  await expect(member.page).toHaveURL(/\/try\/?$/);
});

test("switch: discard and switch ends the session and the unsent message goes with it", async ({ isolated }) => {
  const member = await isolated.member({ name: "Discard member" });
  await typeUnsentMessage(member.page);
  await openAccountMenu(member.page);
  await member.page.getByRole("menuitem", { name: "Switch person" }).click();

  await member.page.getByRole("dialog", { name: "Switch person?" }).getByRole("button", { name: "Discard and switch" }).click();

  await expect(member.page).toHaveURL(/\/sign-in\/?$/);
});

test("switch: no confirm when nothing is unsaved", async ({ isolated }) => {
  const member = await isolated.member({ name: "Quiet member" });
  await member.page.goto("/");
  await openAccountMenu(member.page);
  await member.page.getByRole("menuitem", { name: "Switch person" }).click();

  await expect(member.page).toHaveURL(/\/sign-in\/?$/);
  await expect(member.page.getByRole("dialog")).toHaveCount(0);
});

test("menu: the keyboard opens the account menu, reaches Switch person, and Enter ends the session", async ({
  isolated,
}) => {
  const member = await isolated.member({ name: "Keyboard member" });
  await member.page.goto("/");
  await member.page.getByRole("button", { name: /^Account menu:/ }).focus();
  await member.page.keyboard.press("Enter");

  const switchPerson = member.page.getByRole("menuitem", { name: "Switch person" });
  await expect(switchPerson).toBeVisible();
  // Arrow down until Switch person has focus. It sits above Sign out, so eight presses are more than enough.
  for (let presses = 0; presses < 8 && !(await switchPerson.evaluate((el) => el === document.activeElement)); presses += 1) {
    await member.page.keyboard.press("ArrowDown");
  }
  await expect(switchPerson).toBeFocused();
  await member.page.keyboard.press("Enter");

  await expect(member.page).toHaveURL(/\/sign-in\/?$/);
});

test("sign out: the same confirm asks first, and Escape keeps the work and returns focus to the account button", async ({
  isolated,
}) => {
  const member = await isolated.member({ name: "Escape member" });
  const prompt = await typeUnsentMessage(member.page);
  await openAccountMenu(member.page);
  await member.page.getByRole("menuitem", { name: "Sign out" }).click();

  const dialog = member.page.getByRole("dialog", { name: "Sign out?" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Discard and sign out" })).toBeVisible();
  await member.page.keyboard.press("Escape");

  await expect(dialog).toBeHidden();
  await expect(member.page.getByRole("button", { name: /^Account menu:/ })).toBeFocused();
  await expect(prompt).toHaveValue(UNSENT);
});
