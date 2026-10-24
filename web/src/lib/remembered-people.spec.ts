import { describe, expect, it } from "vitest";
import { leavingTile, type RememberedPerson } from "./remembered-people";

function person(tile: string, name: string, hint: string | null = null): RememberedPerson {
  return {
    tile,
    name,
    avatar: { variant: "v1", initial: name.charAt(0) },
    method: "password",
    provider: null,
    hint,
  };
}

describe("leavingTile", () => {
  const people = [person("t_robin", "Robin Okafor"), person("t_lea", "Léa Okafor")];

  it("names the one remembered entry whose name is the signed-in person's name", () => {
    expect(leavingTile(people, { name: "Léa Okafor", email: "lea@example.test" })).toBe("t_lea");
  });

  it("matches names without regard to case or surrounding spaces", () => {
    expect(leavingTile(people, { name: "  robin OKAFOR ", email: null })).toBe("t_robin");
  });

  it("matches a person with no display name by the local part of their email", () => {
    const list = [person("t_sam", "sam")];
    expect(leavingTile(list, { name: "sam@example.test", email: "sam@example.test" })).toBe("t_sam");
  });

  it("names no one when the signed-in person is not on the list", () => {
    expect(leavingTile(people, { name: "Nasrin Rahimi", email: "nasrin@example.test" })).toBeNull();
  });

  it("names no one when two remembered entries share the name, since either could be the leaver", () => {
    const shared = [person("t_r1", "Robin Okafor", "r…@vivid.fish"), person("t_r2", "Robin Okafor", "r…@other.test")];
    expect(leavingTile(shared, { name: "Robin Okafor", email: "robin@vivid.fish" })).toBeNull();
  });

  it("names no one from an empty list or an anonymous session", () => {
    expect(leavingTile([], { name: "Robin Okafor", email: null })).toBeNull();
    expect(leavingTile(people, { name: null, email: null })).toBeNull();
  });
});
