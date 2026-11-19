import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api";
import { errorCopy, isOutageError, outageCount, readFailureCopy } from "@/lib/error-copy";

describe("read-failure copy", () => {
  it("names the thing and says nothing was changed", () => {
    expect(readFailureCopy("people")).toEqual({
      heading: "Couldn’t load people",
      message: "We couldn’t load people. Nothing was changed. Try again.",
    });
  });

  it("uses the thing in the default message of a server failure", () => {
    const copy = errorCopy(new ApiError("x", "raw server text", 503), { thing: "providers" });
    expect(copy.heading).toBe("Couldn’t load providers");
    expect(copy.message).toBe("We couldn’t load providers. Nothing was changed. Try again.");
    expect(copy.message).not.toContain("raw server text");
    expect(copy.details).toContain("HTTP 503");
  });

  it("keeps a specific message for sign-in and permission failures", () => {
    expect(errorCopy(new ApiError("x", "", 401), { thing: "people" }).message).toBe("Sign in to continue.");
    expect(errorCopy(new ApiError("x", "", 403), { thing: "people" }).recovery).toBe("owner-help");
  });

  it("lets a caller heading win over the thing", () => {
    expect(errorCopy(new ApiError("x", "", 503), { thing: "people", heading: "Couldn’t check status" }).heading).toBe(
      "Couldn’t check status",
    );
  });
});

describe("one outage", () => {
  it("counts a 5xx and a network failure as an outage, not a 4xx", () => {
    expect(isOutageError(new ApiError("x", "", 503))).toBe(true);
    expect(isOutageError(new TypeError("Failed to fetch"))).toBe(true);
    expect(isOutageError(new ApiError("network_unavailable", "", 0))).toBe(true);
    expect(isOutageError(new ApiError("x", "", 403))).toBe(false);
    expect(isOutageError(new ApiError("x", "", 404))).toBe(false);
    expect(isOutageError(null)).toBe(false);
  });

  it("counts the sources that failed together", () => {
    const down = new ApiError("x", "", 503);
    expect(outageCount([down, undefined, null])).toBe(1);
    expect(outageCount([down, new TypeError("offline"), undefined])).toBe(2);
    expect(outageCount([down, new ApiError("x", "", 403)])).toBe(1);
  });
});
