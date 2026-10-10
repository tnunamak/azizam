export type ErrorRecovery = "retry" | "sign-in" | "owner-help" | "refresh";
export interface ErrorCopy {
  heading: string;
  message: string;
  recovery: ErrorRecovery;
  details?: string;
}

/** Present known error contracts without echoing provider responses or secrets. */
export function errorCopy(
  error: unknown,
  options: { heading?: string; role?: "member" | "owner" | "admin" } = {},
): ErrorCopy {
  const record = error && typeof error === "object" ? error : {};
  const code =
    "code" in record && typeof record.code === "string" ? record.code : "";
  const status =
    "status" in record && typeof record.status === "number"
      ? record.status
      : null;
  const safeCode =
    /^[a-z][a-z0-9_]{0,79}$/.test(code) && !/^(opk|oak)_/.test(code)
      ? code
      : "";
  const details =
    [safeCode, status && status >= 100 && status <= 599 ? `HTTP ${status}` : ""]
      .filter(Boolean)
      .join(" · ") || undefined;
  const heading = options.heading ?? "Couldn’t complete this request";
  const result = (message: string, recovery: ErrorRecovery): ErrorCopy => ({
    heading,
    message,
    recovery,
    details,
  });
  if (
    [
      "link_unavailable",
      "invite_unavailable",
      "invite_expired",
      "invite_revoked",
      "invite_consumed",
      "invite_used",
      "invite_not_found",
    ].includes(code)
  ) {
    const link = code === "link_unavailable" ? "link" : "invite link";
    return {
      ...result(
        `This ${link} can no longer be used. Ask the gateway owner for a new ${link}.`,
        "owner-help",
      ),
      heading:
        options.heading ??
        (code === "link_unavailable"
          ? "Couldn’t use this link"
          : "Couldn’t use this invite"),
    };
  }
  if (code === "account_disabled" || code === "credential_revoked")
    return result(
      "Your access is no longer available. Ask the gateway owner to check your access.",
      "owner-help",
    );
  if (
    status === 401 ||
    ["session_expired", "authentication_required"].includes(code)
  )
    return result("Sign in to continue.", "sign-in");
  if (status === 403) {
    return result(
      options.role === "owner" || options.role === "admin"
        ? "This account does not have permission for this action. Sign in with an account that has access."
        : "Your account does not have permission for this action. Ask the gateway owner to check your access.",
      options.role === "owner" || options.role === "admin"
        ? "sign-in"
        : "owner-help",
    );
  }
  if (status === 412 || code === "generation_mismatch")
    return result(
      "The engine configuration changed. Refresh it before saving again.",
      "refresh",
    );
  if (status === 429)
    return result(
      "The gateway is limiting requests. Wait before trying again.",
      "retry",
    );
  if (status !== null && status >= 500)
    return result("The gateway could not complete this request.", "retry");
  if (error instanceof TypeError || code === "network_unavailable")
    return result(
      "The gateway could not be reached. Check your connection.",
      "retry",
    );
  return result(
    "The request could not be completed. Try again; if this continues, ask the gateway owner for help.",
    "retry",
  );
}
