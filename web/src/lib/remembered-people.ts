/**
 * The names this browser remembers (account switcher A3, SECURITY.md section 5). `GET /auth/api/remembered`
 * answers with these and nothing else: no email, no person id, no picture.
 */
export type RememberedPerson = {
  /** Stable for this browser, useless in another. It is what Forget and a sign-in name. */
  tile: string;
  name: string;
  avatar: { variant: string; initial: string };
  method: "password" | "sso";
  /** The provider's display name, for single sign-on people only. */
  provider: string | null;
  /** The start of the address, sent only when two people share a name. */
  hint: string | null;
};

export type RememberedList = {
  /** False while the owner remembers nobody: the list is then always empty. */
  remember: boolean;
  people: RememberedPerson[];
};

/** Who the gateway says is signed in, as `/me/api/session` names them. */
export type SignedInName = { name: string | null; email: string | null };

function fold(value: string | null | undefined): string {
  return (value ?? "").trim().toLocaleLowerCase();
}

/**
 * The tile of the remembered person who is the signed-in person, or null. The session names a person but
 * never a tile, so the match is by name: the display name, or the part of the email before the @ when the
 * account has no display name. Two entries with the same name match nothing, since either could be the one
 * who left, and the browser would then name the wrong person.
 */
export function leavingTile(people: readonly RememberedPerson[], who: SignedInName): string | null {
  const names = [who.name, who.email?.split("@", 1)[0]].map(fold).filter((name) => name !== "");
  const matches = people.filter((person) => names.includes(fold(person.name)));
  return matches.length === 1 ? matches[0].tile : null;
}
