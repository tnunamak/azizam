import { describe, expect, it } from "vitest";

import {
  adminServiceHref,
  adminStatusHref,
  adminTargetHref,
  safeAdminReturnPath,
  safeAuditReturnPath,
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

  it("carries only safe Audit return paths to canonical targets", () => {
    const auditPath = "/admin/audit?actor=usr_1&category=app&event=aud_1";
    expect(safeAdminReturnPath(auditPath)).toBe(auditPath);
    expect(safeAuditReturnPath("/admin/audit/")).toBe("/admin/audit/");
    expect(
      safeAdminReturnPath("https://attacker.invalid/admin/audit"),
    ).toBeNull();
    expect(safeAdminReturnPath("//attacker.invalid/admin/audit")).toBeNull();
    expect(safeAdminReturnPath("/admin/../ui/admin/audit")).toBeNull();
    expect(safeAuditReturnPath("/admin/apps?app=app_1")).toBeNull();
    expect(adminTargetHref("app", "app_1", auditPath)).toBe(
      "/admin/apps?app=app_1&return=%2Fadmin%2Faudit%3Factor%3Dusr_1%26category%3Dapp%26event%3Daud_1",
    );
  });
});
