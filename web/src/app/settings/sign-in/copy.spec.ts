import { describe, expect, it } from "vitest";
import type { Preview, PreviewPerson, SignInRule } from "@/lib/signin-api";
import {
  blockerCaption,
  connectionIntro,
  headline,
  leadingBlocker,
  nameHelp,
  personChange,
  providerLabel,
  rowLabel,
  ruleText,
  savedToast,
  settingSentence,
  testCaption,
  testWorks,
  verdictCopy,
  noGroupsText,
  localCauseText,
  lockedReasonPassword,
  turnOnCaption,
  unlinkedNote,
  openFallbackWarning,
  everyoneAdminWarning,
  onlyInvitedNote,
  providerRowDescription,
  hasPasswordText,
  noPasswordText,
  keysLine,
  issuerLine,
  changeCount,
} from "./copy";

const trusted: SignInRule = {
  id: "r1",
  field: "group",
  values: ["/Trusted"],
  admit: true,
  admin: true,
  plan: null,
};
const plans = [{ id: "p1", name: "Assistant" }];

describe("the provider's name (round 3, N3)", () => {
  it("is the typed Name, else the host of the address", () => {
    expect(providerLabel("Family account", "https://id.example.com/x").name).toBe(
      "Family account",
    );
    expect(providerLabel("  ", "https://id.example.com/realms/home").name).toBe(
      "id.example.com",
    );
    expect(providerLabel("", "").name).toBe("");
  });

  // Every string the page builds from a name, for each of the three cases.
  const strings = (p: ReturnType<typeof providerLabel>) => [
    rowLabel(p),
    connectionIntro(p),
    nameHelp(p),
    headline({
      provider: false,
      password: true,
      hasConnection: true,
      fallback: "closed",
      rules: [],
      label: p,
    }),
    headline({
      provider: true,
      password: true,
      hasConnection: true,
      fallback: "closed",
      rules: [trusted],
      label: p,
    }),
    headline({
      provider: true,
      password: false,
      hasConnection: true,
      fallback: "open",
      rules: [],
      label: p,
    }),
    lockedReasonPassword(p),
    testCaption({
      waiting: true,
      addressBad: false,
      ready: true,
      tested: false,
      testedWhen: "",
      changedSinceLastTest: false,
      p,
    }),
    testCaption({
      waiting: false,
      addressBad: false,
      ready: true,
      tested: true,
      testedWhen: "just now",
      changedSinceLastTest: false,
      p,
    }),
    testWorks(p),
    noGroupsText(p),
    localCauseText("closed", p),
    localCauseText("timeout", p),
    turnOnCaption(p),
    unlinkedNote(p),
    openFallbackWarning(p),
    everyoneAdminWarning(p),
    onlyInvitedNote(p),
    providerRowDescription({ hasAddress: true, p }),
    hasPasswordText(p),
    noPasswordText(p),
    settingSentence("provider_on", p),
    settingSentence("provider_off", p),
    settingSentence("name_changed", p),
    settingSentence("connection_changed", p),
    savedToast({ p, turnedOn: true, forLater: false, removed: [] }),
    savedToast({ p, turnedOn: false, forLater: true, removed: [] }),
    blockerCaption("test_required", "", { live: true, p }),
    blockerCaption("address_invalid", "", { live: true, p }),
    personChange(person({ sign_in: { before: true, after: false, via: "provider" } }), p)!
      .clauses.flat()
      .map((c) => c.text)
      .join(" "),
    verdictCopy(
      { kind: "owner_unlinked", email: "tim@example.com", old_link_stale: true },
      { p, rules: [], plans, justLinked: false },
    ).big,
    verdictCopy(
      { kind: "owner_linked" },
      { p, rules: [], plans, justLinked: true },
    ).big,
  ];

  it("uses the host everywhere when Name is blank, and never 'your provider'", () => {
    const p = providerLabel("", "https://id.example.com/realms/home");
    const all = strings(p);
    for (const s of all) {
      expect(s).not.toMatch(/your provider/i);
      expect(s).not.toMatch(/\ba your\b/i);
      expect(s).not.toMatch(/your own your/i);
    }
    expect(rowLabel(p)).toBe("id.example.com");
    expect(testWorks(p)).toBe("Sign-in with id.example.com works.");
    expect(nameHelp(p)).toContain("“Sign in with id.example.com”");
  });

  it("never starts a sentence with a bare host", () => {
    const p = providerLabel("", "https://id.example.com");
    for (const s of strings(p).filter((x) => x !== p.host)) {
      for (const sentence of s.split(/(?<=\.)\s+/)) {
        expect(sentence).not.toMatch(/^id\.example\.com\b/);
        expect(sentence).not.toMatch(/^Id\.example\.com\b/);
      }
    }
  });

  it("keeps a typed Name exactly, in every string", () => {
    const p = providerLabel("Family account", "https://id.example.com");
    const all = strings(p).join("\n");
    expect(all).toContain("Family account");
    expect(all).not.toMatch(/your provider/i);
    expect(testWorks(p)).toBe("Family account works.");
  });

  it("uses neutral, grammatical wording before an address exists", () => {
    const p = providerLabel("", "");
    expect(rowLabel(p)).toBe("Your provider");
    expect(lockedReasonPassword(p)).toBe(
      "Stays on until your provider is on, so you can still sign in.",
    );
    expect(connectionIntro(p)).toBe(
      "Copy the first address into your provider. Then paste its address below.",
    );
    expect(settingSentence("provider_on", p)).toBe("Your provider sign-in turns on.");
    for (const s of strings(p)) {
      expect(s).not.toMatch(/\ba your\b/i);
      expect(s).not.toMatch(/your own your/i);
      expect(s).not.toMatch(/\{|undefined/);
    }
  });
});

describe("headline", () => {
  const base = {
    password: true,
    hasConnection: true,
    fallback: "closed" as const,
    rules: [trusted],
  };
  it("says who is let in", () => {
    expect(
      headline({ ...base, provider: true, label: providerLabel("Keycloak", "") }),
    ).toBe(
      "People sign in with a password or with Keycloak. Keycloak lets in /Trusted, and people you invite. Everyone else is kept out.",
    );
  });
  it("says a saved provider is off, and what to do when none is saved", () => {
    expect(
      headline({ ...base, provider: false, label: providerLabel("Keycloak", "") }),
    ).toBe("People sign in with a password. Keycloak is saved but off.");
    expect(
      headline({
        ...base,
        provider: false,
        hasConnection: false,
        label: providerLabel("", ""),
      }),
    ).toBe(
      "People sign in with a password. To add another way to sign in, connect a provider below.",
    );
  });
  it("summarises many rules and the open fallback", () => {
    const many: SignInRule[] = ["/a", "/b", "/c"].map((v, i) => ({
      ...trusted,
      id: `r${i}`,
      values: [v],
    }));
    expect(
      headline({
        ...base,
        rules: many,
        provider: true,
        label: providerLabel("Keycloak", ""),
      }),
    ).toContain("lets in the people your rules name");
    expect(
      headline({
        ...base,
        fallback: "open",
        provider: true,
        label: providerLabel("Keycloak", ""),
      }),
    ).toContain("Anyone else from Keycloak gets in but can use nothing until you give them a plan.");
  });
});

describe("rule text", () => {
  it("names plans, never ids", () => {
    expect(ruleText(trusted, plans)).toBe("Group is /Trusted → let in, administrator");
    expect(
      ruleText(
        { ...trusted, admin: false, plan: "p1", values: ["/a", "/b"] },
        plans,
      ),
    ).toBe("Group is one of /a, /b → let in, plan Assistant");
    expect(ruleText({ ...trusted, field: "everyone", values: [] }, plans)).toBe(
      "Everyone → let in, administrator",
    );
  });
});

function person(over: Partial<PreviewPerson>): PreviewPerson {
  return {
    id: "simon",
    name: "Simon Agent",
    sign_in: { before: true, after: true, via: "provider" },
    admin: { before: false, after: false },
    plan: { before: null, after: null },
    admin_keys: 0,
    ...over,
  };
}

describe("what changes, per person", () => {
  const p = providerLabel("Keycloak", "");
  const line = (c: ReturnType<typeof personChange>) =>
    c!.clauses.map((cl) => cl.map((x) => x.text).join(" ")).join(", ");
  it("says a person loses administrator now and is signed out, and how many keys", () => {
    const c = personChange(
      person({
        sign_in: { before: true, after: false, via: "provider" },
        admin: { before: true, after: false },
        admin_keys: 2,
      }),
      p,
    );
    expect(line(c)).toBe(
      "can no longer sign in (uses Keycloak), loses administrator now and is signed out",
    );
    expect(c!.keys).toBe(2);
    expect(keysLine("Simon Agent", 2, false)).toBe(
      "2 admin keys Simon Agent made. They are revoked when you save.",
    );
    expect(keysLine("Simon Agent", 1, true)).toBe(
      "1 admin key Simon Agent made. They keep working.",
    );
  });
  it("says a password user can no longer sign in with 'a password'", () => {
    expect(
      line(
        personChange(
          person({ sign_in: { before: true, after: false, via: "password" } }),
          p,
        ),
      ),
    ).toBe("can no longer sign in (uses a password)");
  });
  it("words plan changes", () => {
    const a = { id: "a", name: "Everything" };
    const b = { id: "b", name: "Assistant" };
    expect(line(personChange(person({ plan: { before: null, after: b } }), p))).toBe(
      "gets plan Assistant (now: no plan)",
    );
    expect(line(personChange(person({ plan: { before: a, after: null } }), p))).toBe(
      "loses plan Everything",
    );
    expect(line(personChange(person({ plan: { before: a, after: b } }), p))).toBe(
      "plan Everything → Assistant",
    );
  });
  it("returns nothing for a person who is not affected", () => {
    expect(personChange(person({}), p)).toBeNull();
  });
});

describe("issuer change (P7)", () => {
  it("adds that people are checked by the rules again when they sign in", () => {
    const l = issuerLine({ old_host: "id.a.test", new_host: "id.b.test", people: 3 });
    expect(l.who).toBe("Everyone who signed in at id.a.test (3 people):");
    expect(l.what).toBe("become new people at id.b.test");
    expect(l.rest).toContain("link yourself again after you save.");
    expect(l.rest).toContain("They are checked by your rules again when they sign in.");
  });
});

describe("setting sentences and the change count", () => {
  const p = providerLabel("Keycloak", "");
  it("words each setting", () => {
    expect(settingSentence("password_off", p)).toBe(
      "Email and password sign-in turns off. You keep your own password.",
    );
    expect(settingSentence("fallback_open", p)).toBe(
      "Everyone else is now let in with no access.",
    );
    expect(settingSentence("connection_changed", p)).toBe(
      "The connection settings change. Keycloak keeps working with the old settings until you save.",
    );
  });
  it("counts every line and every settings sentence", () => {
    const preview: Preview = {
      people: [person({}), person({ id: "x" })],
      unaffected: [],
      issuer_change: { old_host: "a", new_host: "b", people: 1 },
      settings: ["provider_off", "name_changed"],
      blockers: [],
      test_verdict: null,
      rule_matches: {},
    };
    expect(changeCount(preview)).toBe(5);
    expect(changeCount(undefined)).toBe(0);
  });
});

describe("blockers", () => {
  const p = providerLabel("Keycloak", "");
  it("orders the leading blocker for a live and a new provider", () => {
    const blockers: Preview["blockers"] = [
      { code: "test_required", message: "x" },
      { code: "address_invalid", message: "y" },
    ];
    expect(leadingBlocker(blockers, true)?.code).toBe("address_invalid");
    expect(leadingBlocker(blockers, false)?.code).toBe("test_required");
    expect(
      leadingBlocker(
        [...blockers, { code: "incomplete_rule", message: "z" }],
        true,
      )?.code,
    ).toBe("incomplete_rule");
  });
  it("words each caption", () => {
    expect(blockerCaption("incomplete_rule", "", { live: true, p })).toBe(
      "Finish or remove the new rule first.",
    );
    expect(blockerCaption("test_required", "", { live: true, p })).toBe(
      "Test sign-in again before you save. Keycloak keeps working with the old settings until then.",
    );
    expect(blockerCaption("test_required", "", { live: false, p })).toBe(
      "Test sign-in first",
    );
    expect(blockerCaption("address_invalid", "", { live: true, p })).toBe(
      "Fix the provider address first. Keycloak keeps working with the old settings until then.",
    );
    expect(blockerCaption("both_methods_off", "Keep one on.", { live: true, p })).toBe(
      "Keep one on.",
    );
  });
});

describe("test verdicts (the server decides; the page words them)", () => {
  const p = providerLabel("Keycloak", "");
  const ctx = (rules: SignInRule[] = [trusted]) => ({
    p,
    rules,
    plans,
    justLinked: false,
  });
  it("words an invited person who has never signed in (P2)", () => {
    const v = verdictCopy(
      {
        kind: "new",
        email: "mum@example.com",
        host: "id.example.com",
        issuer_changed: false,
        outcome: {
          admitted: true,
          admin: false,
          plan: null,
          rule_ids: [],
          invited: true,
        },
      },
      ctx(),
    );
    expect(v.big).toBe(
      "mum@example.com has not signed in before. They would be let in, with no plan.",
    );
    expect(v.why).toBe("Because you invited them.");
  });
  it("words a kept-out new person", () => {
    const v = verdictCopy(
      {
        kind: "new",
        email: "mum@example.com",
        host: "id.example.com",
        issuer_changed: true,
        outcome: {
          admitted: false,
          admin: false,
          plan: null,
          rule_ids: [],
          invited: false,
        },
      },
      ctx(),
    );
    expect(v.big).toBe(
      "mum@example.com has not signed in at id.example.com. They would be kept out. No rule lets them in, and you keep out people you have not invited.",
    );
    expect(v.why).toBeUndefined();
  });
  it("maps rule ids to the form's rule texts for 'Because:'", () => {
    const v = verdictCopy(
      {
        kind: "person",
        person: { id: "simon", name: "Simon Agent" },
        outcome: {
          admitted: true,
          admin: true,
          plan: { id: "p1", name: "Assistant" },
          rule_ids: ["r1", "gone"],
          invited: false,
        },
      },
      ctx(),
    );
    expect(v.big).toBe(
      "This is Simon Agent’s account. Simon Agent would be let in as an administrator, with plan Assistant.",
    );
    expect(v.why).toBe("Because: Group is /Trusted → let in, administrator.");
  });
  it("offers Link only for an unlinked owner, and names the identity", () => {
    const v = verdictCopy(
      { kind: "owner_unlinked", email: "tim@example.com", old_link_stale: false },
      ctx(),
    );
    expect(v.link).toBe("Link tim@example.com to my account");
    expect(v.big).toBe("tim@example.com at Keycloak is not linked to you yet.");
    expect(
      verdictCopy({ kind: "owner_linked" }, ctx()).link,
    ).toBeUndefined();
  });
});

describe("saved toast", () => {
  const p = providerLabel("Keycloak", "");
  it("says what happened to admin keys", () => {
    expect(
      savedToast({
        p,
        turnedOn: false,
        forLater: false,
        removed: [
          { name: "Simon Agent", keys_revoked: 2, keys_kept: 0 },
          { name: "Mum", keys_revoked: 0, keys_kept: 1 },
        ],
      }),
    ).toBe(
      "Saved. Simon Agent, Mum lost administrator and were signed out. 2 admin keys Simon Agent made were revoked. 1 admin key Mum made still works.",
    );
  });
  it("covers turn on, save for later and a plain save", () => {
    expect(savedToast({ p, turnedOn: true, forLater: false, removed: [] })).toBe(
      "Saved. Keycloak is on. A sign-in button now shows on the sign-in page.",
    );
    expect(savedToast({ p, turnedOn: false, forLater: true, removed: [] })).toBe(
      "Saved. Keycloak stays off until a test passes.",
    );
    expect(savedToast({ p, turnedOn: false, forLater: false, removed: [] })).toBe(
      "Saved. Changes apply to the next sign-in.",
    );
  });
});
