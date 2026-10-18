"""A keyed digest for records the gateway must recognise later but must not let a reader guess.

An installed extension package keeps a proof that each record it wrote is unchanged (``gateway/extension_packages.py``),
and the package index is returned to callers. A bare hash of a record is a guess oracle for any low-entropy value the
record holds, so the proof is an HMAC under a key derived from the gateway's own key file (the file that protects the
secret store). The key never leaves this module, and nothing here reads or returns a stored secret.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from gateway import secret_store

INSTALL_DIGEST_LABEL = b"azizam/extension-package-install-digest/v3"


def install_key(db_path: Path) -> bytes:
    """The key of the install proofs of the gateway whose database is ``db_path`` (created with the key file if absent)."""
    master = secret_store.load_or_create_secret_key(Path(db_path).parent / secret_store.DEFAULT_KEY_FILE)
    return hmac.new(master, INSTALL_DIGEST_LABEL, hashlib.sha256).digest()


def mac(key: bytes, data: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()
