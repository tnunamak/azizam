import assert from "node:assert/strict";
import test from "node:test";

import {
  adminServiceHref,
  adminStatusHref,
  adminTargetHref,
} from "./admin-links";

test("admin target links accept only canonical attention tuples", () => {
  assert.equal(adminTargetHref("app", "app a"), "/admin/apps?app=app%20a");
  assert.equal(
    adminTargetHref("profile", "prof_1"),
    "/admin/profiles?profile=prof_1",
  );
  assert.equal(
    adminTargetHref("service", "svc_1"),
    "/admin/services?service=svc_1",
  );
  assert.equal(adminTargetHref("audit", "aud_1"), "/admin/audit?event=aud_1");
  assert.equal(adminTargetHref("backend", "svc_1"), null);
  assert.equal(adminTargetHref("app_profile", "prof_1"), null);
});

test("diagnostic status links stay distinct from service configuration links", () => {
  assert.equal(adminStatusHref(), "/admin/status");
  assert.equal(adminStatusHref("svc_1"), "/admin/status?service=svc_1");
  assert.equal(adminServiceHref("svc_1"), "/admin/services?service=svc_1");
});
