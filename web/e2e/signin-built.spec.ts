import { mkdirSync } from "node:fs";
import path from "node:path";
import { expect, test } from "./fixtures/isolation";
import { fulfilJson } from "./fixtures/intercept";
import { answerSignInOptions, signedOut } from "./fixtures/signed-out";

/*
 * The built signed-out front door, saved for the design side-by-side check: three sign-in methods, with and without
 * remembered people, at 1440 and 390, in light and dark. The household's own names are answered here, because the
 * fixture has no identity provider to remember people through. Set SIGNIN_SHOTS_DIR to save the pictures; otherwise
 * these cases are skipped.
 */
const SHOTS = process.env.SIGNIN_SHOTS_DIR;

const METHODS = {
  "sso-only": { built_in: false, oidc: true },
  "password-only": { built_in: true, oidc: false },
  both: { built_in: true, oidc: true },
} as const;

/** The people a browser remembers, as the gateway lists them for each method. */
function rememberedFor(mode: keyof typeof METHODS) {
  const robin = { tile: "tile-robin", name: "Robin Okafor", avatar: { variant: "v1", initial: "R" }, hint: null, provider: null };
  const june = { tile: "tile-june", name: "June Okafor", avatar: { variant: "v4", initial: "J" }, hint: null, provider: null };
  if (mode === "sso-only")
    return [
      { ...robin, method: "sso", provider: "Okafor ID" },
      { ...june, method: "sso", provider: "Okafor ID" },
    ];
  if (mode === "password-only") return [
    { ...robin, method: "password" },
    { ...june, method: "password" },
  ];
  return [
    { ...robin, method: "password" },
    { ...june, method: "sso", provider: "Okafor ID" },
  ];
}

test.describe("built sign-in pages for the design side-by-side check", () => {
  test.skip(!SHOTS, "set SIGNIN_SHOTS_DIR to save the built sign-in pages");
  for (const mode of Object.keys(METHODS) as (keyof typeof METHODS)[]) {
    for (const remembered of [false, true]) {
      const name = `signin-${mode}${remembered ? "-remembered" : ""}`;
      test(`saves the ${mode} sign-in${remembered ? " with remembered people" : ""} at 1440 and 390, light and dark`, async ({ browser, baseURL }) => {
        mkdirSync(SHOTS!, { recursive: true });
        for (const width of [1440, 390]) {
          for (const theme of ["light", "dark"] as const) {
            const height = width === 390 ? 844 : 900;
            const { context, page } = await signedOut(browser, baseURL, { width, height });
            try {
              await context.addInitScript((value) => localStorage.setItem("theme", value), theme);
              await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
              await page.route("**/auth/api/options*", (route) => answerSignInOptions(route, METHODS[mode]));
              await page.route("**/auth/api/remembered", (route) =>
                fulfilJson(route, { remember: true, people: remembered ? rememberedFor(mode) : [] }),
              );
              await page.goto("/");
              // The loaded page: its heading names the household, or the people who remember it. The loading state's
              // heading is only "Sign in".
              await expect(page.getByRole("heading", { level: 1 })).toHaveText(/^(Sign in to |Who’s signing in\?)/);
              await page.evaluate(() => document.fonts.ready);
              await page.screenshot({ path: path.join(SHOTS!, `${name}-${width}${theme === "dark" ? "-dark" : ""}.png`) });
            } finally {
              await context.close();
            }
          }
        }
      });
    }
  }
});
