import { describe, expect, it } from "vitest";

import {
  adminServiceHref,
  adminStatusHref,
  adminTargetHref,
} from "../../src/lib/admin-links";

describe("admin links", () => {
  it("accepts only canonical attention tuples", () => {
    expect(adminTargetHref("app", "app a")).toBe("/admin/apps?app=app%20a");
    expect(adminTargetHref("profile", "prof_1")).toBe(
      "/admin/profiles?profile=prof_1",
    );
    expect(adminTargetHref("service", "svc_1")).toBe(
      "/admin/services?service=svc_1",
    );
    expect(adminTargetHref("audit", "aud_1")).toBe("/admin/audit?event=aud_1");
    expect(adminTargetHref("backend", "svc_1")).toBeNull();
    expect(adminTargetHref("app_profile", "prof_1")).toBeNull();
  });

  it("keeps diagnosis distinct from service configuration", () => {
    expect(adminStatusHref()).toBe("/admin/status");
    expect(adminStatusHref("svc_1")).toBe("/admin/status?service=svc_1");
    expect(adminServiceHref("svc_1")).toBe("/admin/services?service=svc_1");
  });
});
