import { describe, expect, it } from "vitest";
import { safeContinuePath } from "@/app/sign-in/auth-api";
describe("sign-in return paths", () => {
  it.each(["/ui/engines/?service=a", "/status/raw?detail=1#gpu", "/setup/"])(
    "keeps a supported destination %s",
    (path) => {
      expect(safeContinuePath(path)).toBe(path);
    },
  );
  it.each([
    "https://elsewhere.test",
    "//elsewhere.test",
    "/\\elsewhere.test",
    "/ui/../logout",
    "/ui/home/\n",
    "/admin/services",
  ])("rejects an unsafe or unsupported destination %s", (path) => {
    expect(safeContinuePath(path)).toBe("/ui/home/");
  });
});
