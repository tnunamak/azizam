import { describe, expect, it } from "vitest";
import {
  afterFrom,
  notFreshFor,
  orderPeople,
  returnToFor,
  statusFor,
} from "./sign-in-status";

describe("afterFrom", () => {
  it("reads only the four endings the sign-in page knows", () => {
    expect(afterFrom("?after=switch")).toBe("switch");
    expect(afterFrom("?after=sign-out")).toBe("sign-out");
    expect(afterFrom("?after=everywhere")).toBe("everywhere");
    expect(afterFrom("?after=provider-sign-out")).toBe("provider-sign-out");
  });

  it("ignores an unknown or missing ending", () => {
    expect(afterFrom("")).toBeNull();
    expect(afterFrom("?after=lock")).toBeNull();
    expect(afterFrom("?after=")).toBeNull();
    expect(afterFrom("?return_to=%2Fusage%2F")).toBeNull();
  });
});

describe("orderPeople", () => {
  const people = [{ tile: "t_robin" }, { tile: "t_nasrin" }, { tile: "t_lea" }];

  it("keeps the gateway's order when nobody just left", () => {
    expect(orderPeople(people, null).map((p) => p.tile)).toEqual(["t_robin", "t_nasrin", "t_lea"]);
  });

  it("moves the person who just switched out to the end, and keeps everyone else in order", () => {
    expect(orderPeople(people, "t_robin").map((p) => p.tile)).toEqual(["t_nasrin", "t_lea", "t_robin"]);
  });

  it("changes nothing when the person who left is not on the list", () => {
    expect(orderPeople(people, "t_sam").map((p) => p.tile)).toEqual(["t_robin", "t_nasrin", "t_lea"]);
  });
});

describe("statusFor", () => {
  it("says who was signed out by a switch, and how to come back, when the name is on the list", () => {
    expect(statusFor("switch", { leaver: { name: "Robin Okafor", tile: "t_robin" } })).toEqual({
      text: "Robin Okafor is signed out. To come back, sign in again.",
      forget: null,
    });
  });

  it("says only that the person is signed out when no name is shown", () => {
    expect(statusFor("switch", { leaver: null })).toEqual({
      text: "You’re signed out. To come back, sign in again.",
      forget: null,
    });
  });

  it("offers to forget the person who signed out, by their name", () => {
    expect(statusFor("sign-out", { leaver: { name: "Léa Okafor", tile: "t_lea" } })).toEqual({
      text: "You’re signed out.",
      forget: { name: "Léa Okafor", tile: "t_lea" },
    });
  });

  it("offers no forget line when the person is not on the list", () => {
    expect(statusFor("sign-out", { leaver: null })).toEqual({ text: "You’re signed out.", forget: null });
  });

  it("confirms a sign-out everywhere, with no forget offer", () => {
    expect(statusFor("everywhere", { leaver: null })).toEqual({
      text: "You’re signed out everywhere.",
      forget: null,
    });
  });

  it("names the provider after a provider sign-out, and asks for the name to be chosen", () => {
    expect(statusFor("provider-sign-out", { provider: "Family login" })).toEqual({
      text: "You’re signed out of Family login. Choose your name to sign in again.",
      forget: null,
    });
  });

  it("confirms a forgotten name", () => {
    expect(statusFor("forgot", { name: "Léa Okafor" })).toEqual({
      text: "Léa Okafor is forgotten on this browser.",
      forget: null,
    });
  });

  it("confirms that everyone has been forgotten", () => {
    expect(statusFor("all-forgotten")).toEqual({ text: "Everyone is forgotten on this browser.", forget: null });
  });
});

describe("returnToFor", () => {
  it("keeps a deep link for an ordinary sign-in", () => {
    expect(returnToFor({ requested: "/usage/?receipt=one", after: null, signedOut: false, fromTile: false })).toBe(
      "/usage/?receipt=one",
    );
  });

  it("drops the link after a switch or a sign-out", () => {
    expect(returnToFor({ requested: "/usage/", after: "switch", signedOut: false, fromTile: false })).toBe("/");
    expect(returnToFor({ requested: "/usage/", after: "sign-out", signedOut: false, fromTile: false })).toBe("/");
  });

  it("drops the link while a sign-out in this browser has not been followed by a sign-in", () => {
    expect(returnToFor({ requested: "/usage/", after: null, signedOut: true, fromTile: false })).toBe("/");
  });

  it("always sends a remembered person Home", () => {
    expect(returnToFor({ requested: "/invite/", after: null, signedOut: false, fromTile: true })).toBe("/");
  });

  it("falls back to Home when there is no link", () => {
    expect(returnToFor({ requested: null, after: null, signedOut: false, fromTile: false })).toBe("/");
  });
});

describe("notFreshFor (a sign-in refused as not fresh, S8)", () => {
  it("offers the provider's sign-out when the provider publishes a sign-out address", () => {
    expect(notFreshFor("Keycloak", true)).toEqual({
      title: "Sign out of Keycloak first",
      body: [
        "Azizam couldn’t confirm that Keycloak just asked for a password, so it stopped. Someone may still be signed in to Keycloak in this browser.",
        "Signing out of Keycloak also signs out of other apps that use it in this browser.",
      ],
    });
  });

  it("says who can sign out when the provider has no sign-out address (S8b)", () => {
    expect(notFreshFor("Keycloak", false)).toEqual({
      title: "Sign out of Keycloak first",
      body: [
        "Azizam couldn’t confirm that Keycloak just asked for a password, so it stopped. Ask the person who used Keycloak last to sign out of it. Or open Keycloak in a new tab, choose Sign out, and open nothing else there.",
        "If neither works, ask the person who runs Azizam.",
      ],
    });
  });
});
