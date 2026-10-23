/**
 * What the sign-in page says on arrival, and where the list puts people (account switcher W4; DESIGN.md
 * 4.5, 4.10). These functions are the copy and the order. The page reads the address and the browser's
 * record; it does not decide the wording itself.
 */

/** How the page was reached after a session ended here: `?after=` (SECURITY.md section 3). */
export type After = "switch" | "sign-out" | "everywhere" | "provider-sign-out";

const AFTERS: readonly After[] = ["switch", "sign-out", "everywhere", "provider-sign-out"];

/** The ending named in the address, or null for any other or missing value. */
export function afterFrom(search: string): After | null {
  const value = new URLSearchParams(search).get("after");
  return AFTERS.find((after) => after === value) ?? null;
}

/**
 * The gateway's order, with the person who just switched out moved to the end, so the next person does not
 * scroll past them. Everyone else keeps the order they came in. Naming no one, or naming someone not on the
 * list, changes nothing.
 */
export function orderPeople<T extends { tile: string }>(people: readonly T[], leaving: string | null): T[] {
  if (leaving === null || !people.some((person) => person.tile === leaving)) return [...people];
  return [...people.filter((person) => person.tile !== leaving), ...people.filter((person) => person.tile === leaving)];
}

export type LeftPerson = { tile: string; name: string };

export type StatusKind = After | "forgot" | "all-forgotten";

export type StatusDetail = {
  /** The person who just left, when they are on the list. Null shows the copy that names nobody. */
  leaver?: LeftPerson | null;
  /** The provider's display name, after a provider sign-out. */
  provider?: string;
  /** The name just forgotten. */
  name?: string;
};

export type Status = {
  text: string;
  /** Offered after a sign-out, when the person who left is on the list: forget them here. */
  forget: LeftPerson | null;
};

/** The line the page shows at the top of the list or form. The wording is DESIGN.md 4.5, word for word. */
export function statusFor(kind: StatusKind, detail: StatusDetail = {}): Status {
  switch (kind) {
    case "switch":
      return {
        text: detail.leaver
          ? `${detail.leaver.name} is signed out. To come back, sign in again.`
          : "You’re signed out. To come back, sign in again.",
        forget: null,
      };
    case "sign-out":
      return { text: "You’re signed out.", forget: detail.leaver ?? null };
    case "everywhere":
      return { text: "You’re signed out everywhere.", forget: null };
    case "provider-sign-out":
      return {
        text: `You’re signed out of ${detail.provider ?? "your sign-in provider"}. Choose your name to sign in again.`,
        forget: null,
      };
    case "forgot":
      return { text: `${detail.name ?? "This person"} is forgotten on this browser.`, forget: null };
    case "all-forgotten":
      return { text: "Everyone is forgotten on this browser.", forget: null };
  }
}

/**
 * Where the sign-in lands after a password or a single sign-on, and whether the link the page came in with is
 * kept. A link from an earlier person's page never reaches the next person: not after a switch or a sign-out
 * here, not while this browser's last sign-out has no sign-in after it, and never for a remembered person,
 * who always goes Home. A plain sign-in with no ending keeps its link, as before.
 */
export function returnToFor(input: {
  requested: string | null;
  after: After | null;
  signedOut: boolean;
  fromTile: boolean;
}): string {
  if (input.after !== null || input.signedOut || input.fromTile) return "/";
  return input.requested || "/";
}
