"""Local owner password verifiers and persistent browser-session signing key.

The gateway's web-first first-run flow needs two small primitives that don't
belong to OIDC or runtime keys:

* A way to verify a password the operator picked in their browser, so the
  break-glass admin account doesn't need OIDC and doesn't ship as a CLI-only
  recipe.
* A persistent random key for signing the browser admin cookie when the
  operator hasn't supplied one via env/config. The happy path is "start the
  service, open /setup" — requiring an env var to even land on the page is
  exactly the friction the MVP is trying to remove.

Both pieces are intentionally tiny and have no Flask/FastAPI imports. The
HTTP routes in :mod:`proxy` compose them.

Password verifiers use ``hashlib.pbkdf2_hmac`` (stdlib, no extra deps). The
default iteration count is chosen to give roughly one OWASP-recommended pass
on contemporary hardware while still being cheap enough for the single-host
local-owner sign-in. It is recorded inside the verifier so we can bump the
default without breaking old verifiers.

The signing-key file lives next to ``admin.db`` so a single ``data/`` copy
covers the whole bootstrap surface, and it's written with restrictive
permissions to avoid accidentally publishing it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Final

__all__ = [
    "DEFAULT_PBKDF2_ITERATIONS",
    "PASSWORD_VERIFIER_VERSION",
    "hash_password",
    "verify_password",
    "load_or_create_signing_key",
]


PASSWORD_VERIFIER_VERSION: Final[str] = "pbkdf2_sha256$v1"

# 600k matches the floor OWASP currently recommends for pbkdf2-sha256. The
# first-run sign-in happens once per browser session, so a few hundred
# milliseconds of CPU here is irrelevant to runtime latency.
DEFAULT_PBKDF2_ITERATIONS: Final[int] = 600_000

_SALT_BYTES: Final[int] = 16
_KEY_BYTES: Final[int] = 32
_SIGNING_KEY_BYTES: Final[int] = 48  # 384 bits — comfortably above the 32-byte floor


def hash_password(
    password: str,
    *,
    iterations: int = DEFAULT_PBKDF2_ITERATIONS,
) -> str:
    """Return a self-describing verifier string for ``password``.

    Format: ``pbkdf2_sha256$v1$<iterations>$<salt_hex>$<hash_hex>``. The
    iteration count lives inside the verifier so we can raise the default
    later without invalidating older accounts.
    """
    if not isinstance(password, str) or not password or len(password) > 1024:
        raise ValueError("Enter a password of up to 1,024 characters.")
    if iterations < 1_000:
        raise ValueError("iteration count is too low to be safe")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=_KEY_BYTES
    )
    return f"{PASSWORD_VERIFIER_VERSION}${iterations}${salt.hex()}${derived.hex()}"


def verify_password(password: str, verifier: str) -> bool:
    """Constant-time check of a password against a stored verifier."""
    if not isinstance(password, str) or not password:
        return False
    if not isinstance(verifier, str) or not verifier:
        return False
    try:
        scheme, version, iterations_str, salt_hex, hash_hex = verifier.split("$", 4)
        if f"{scheme}${version}" != PASSWORD_VERIFIER_VERSION:
            return False
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    if iterations < 1_000:
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=len(expected)
    )
    return hmac.compare_digest(derived, expected)


def load_or_create_signing_key(path: Path) -> bytes:
    """Return a persistent random signing key, creating it if missing.

    Writes ``path`` with ``0o600`` permissions so other users on the host
    can't read it. The directory is created with ``0o700`` if needed. If the
    file exists but is too short to be safe, it's regenerated rather than
    silently used.
    """
    path = Path(path)
    if path.exists():
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(
                f"could not read browser session signing key at {path}: {exc}"
            ) from exc
        if len(raw) >= 32:
            return raw
        # Too short to be safe — fall through and regenerate.

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        # Best effort — some filesystems don't support chmod (mounted volumes
        # on Windows, NFS without root-squash off, etc.). The dir already
        # existed in those cases.
        pass

    key = secrets.token_bytes(_SIGNING_KEY_BYTES)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        # O_CREAT|O_EXCL avoids racing two startups on the same file. If the
        # tmp file already exists it's stale from a previous crashed write;
        # remove and retry once.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(str(tmp), flags, 0o600)
        except FileExistsError:
            tmp.unlink(missing_ok=True)
            fd = os.open(str(tmp), flags, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        raise RuntimeError(
            f"could not write browser session signing key to {path}: {exc}"
        ) from exc
    return key
