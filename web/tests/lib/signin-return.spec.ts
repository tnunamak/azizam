import { describe, expect, it } from "vitest";
import { safeContinuePath } from "@/app/sign-in/auth-api";
// The gateway's sanitizer (gateway/admin_oidc.safe_browser_return_to) runs the
// same table, so the client and server agree on every case.
import fixture from "../../../tests/fixtures/return_paths.json";

describe("sign-in return paths", () => {
  it("has shared cases", () => {
    expect(fixture.cases.length).toBeGreaterThan(0);
  });
  it.each(fixture.cases as [string, string][])(
    "sends %j to %j",
    (input, expected) => {
      expect(safeContinuePath(input)).toBe(expected);
    },
  );
  it.each([
    "/engines/?service=a",
    "/status/raw?detail=1#gpu",
    "/setup/",
    "/invite/#token=abc",
  ])("keeps a supported destination %s", (path) => {
    expect(safeContinuePath(path)).toBe(path);
  });
  it.each([
    "/library/./",
    "/library/%2E/",
    "/library//",
    "/constructor/",
    "/library/%zz/",
    "/../logout",
    "/library/\n",
  ])("rejects %j", (path) => {
    expect(safeContinuePath(path)).toBe("/");
  });
});
