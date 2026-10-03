# Built-in accounts and recovery links

Date: 2026-10-03. Scope: the People implementation lane.

The local corpus index was checked first, together with the gateway's existing
local-owner, OIDC, browser-cookie, profile and user-ownership implementation.
No prior account-password implementation note was present in this worktree's
research index. Existing code already supplies signed cookies and CSRF tokens;
introducing a second session format would add a second authorization path.

## Sources read and decisions

| Source | Relevant finding | Implementation |
| --- | --- | --- |
| [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html) | Memory-hard password hashing; scrypt is the alternative when Argon2id is unavailable. Its recommended scrypt parameters are N=2^17, r=8, p=1. Legacy PBKDF2 guidance requires 600,000 iterations. | Use stdlib scrypt with these fixed parameters, random 16-byte salts, 32-byte outputs and constant-time comparison. Existing local-owner PBKDF2 verifiers remain readable. Limit concurrent expensive password work to two operations. |
| [OWASP Forgot Password](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html) | Recovery tokens need sufficient entropy, secure storage, a limited lifetime and one use. Recovery should invalidate existing sessions. | Owner creates a random 256-bit link; only SHA-256 is persisted. Consumption and verifier replacement are one SQLite transaction; reset removes registered sessions and requires fresh registered sessions. |

The password source explicitly recommends: “If Argon2id is not available, use
scrypt”. The recovery source calls for tokens to be “Single use and expire after
an appropriate period.” These are the only verbatim excerpts retained here.

## Boundaries, alternatives and caveats

- No email delivery service is introduced. The owner shares a recovery or invite
  link through their chosen channel. This matches the requested household workflow.
- API-created invitation tokens use a domain-separated HMAC of the persistent
  browser signing key and a random link ID; the database retains only their
  hashes. Owners can recover a pending link for copying without storing the
  token. Password-reset tokens remain independently random and shown once.
  Tokens live in URL fragments on the browser pages, so navigation and Referer
  headers do not send them to servers. Inspection necessarily sends a token to
  the JSON endpoint; account API responses use no-store and no-referrer.
- Anonymous and known-account login attempts perform the same scrypt work for
  scrypt accounts. Legacy owner accounts retain their existing PBKDF2 work cost.
  Persistent per-email and per-IP throttles constrain guessing across workers.
- OIDC-targeted invitations require a verified email claim recorded by the
  validated callback. An arbitrary request cannot assert verification.
- No passkey library is already present in the locked dependencies. Passkeys
  were considered and deferred; adding a WebAuthn dependency, migrations and
  credential-recovery UI would exceed the brief's optional small implementation.
- No monthly request total is claimed from a diagnostic store that retains only
  14 days. The People API reports null until a complete monthly measurement
  source is available. No rate-limit policy is fabricated in the plan editor.
- Removal preserves an identity tombstone for revocation and historical app /
  receipt attribution. It removes the member from People and deletes their
  password verifier, while revoking their apps, keys and sessions. Physical
  deletion would allow old OIDC identities to return with fresh access.
