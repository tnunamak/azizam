"""The rule that finds a credential in a command that was stored as plain text.

Decision D26 moves secrets out of records by structure: a secret is an entry, never a string that looks like
one. This module exists for the two places that still meet a plain command: the one-time migration of a record
written before D26, and the ingestion guard that refuses a command line that looks as if it carries a
credential (the writer is told to send that argument as a secret entry instead). It decides nothing about what
a view shows: views are built from structure (``gateway/projection.py``) and never read a command's text.

The rule reads the flag, never an engine: an argument is a credential when it follows a flag whose name has a
whole credential word in it, sits after ``=`` in such a flag or in ``NAME=value``, is attached to a short
credential flag (``-pSECRET``), or is a URL that carries user info or a query.
"""

from __future__ import annotations

import re
from typing import Any

# A part of a flag name that makes the flag a credential. A flag is split into parts at ``-``, ``_`` and a
# lower-to-upper change, so ``--api-key`` and ``--auth-token`` match and ``--tokenizer``, ``--max-tokens`` and
# ``--keyfile`` (a path) do not.
CREDENTIAL_FLAG_WORDS = frozenset({
    "token", "secret", "key", "apikey", "privatekey", "password", "passwd", "pass", "passphrase", "pwd",
    "auth", "authorization", "bearer", "credential", "credentials", "cookie", "jwt", "pin", "otp",
    "session", "sessionid", "sig", "signature", "psk",
    # the review of the first version found these ordinary spellings of the same idea
    "login", "cred", "creds", "pw", "tok", "apitoken", "authtoken", "authkey", "oauth", "sas", "conn", "license",
    "licence", "webhook", "account", "dsn",
})
# Flag names that are a credential only as a whole (``--connection-string``), not by one of their words.
CREDENTIAL_FLAG_NAMES = frozenset({"connectionstring", "connstring", "accesskey", "licensekey", "ngrokauthtoken", "hftoken"})
CREDENTIAL_SHORT_FLAGS = frozenset("pPkK")
USER_FLAGS = frozenset({"-u", "--user"})
# A flag whose value is a ``Name: value`` header or a ``NAME=value`` variable: the value is a credential by default,
# because what it carries (an Authorization header, an environment variable) is not told by the flag's name.
VALUE_FLAGS = frozenset({"-H", "--header", "-e", "--env"})

_ATTACHED_SHORT = re.compile(r"^-([pPkKHe])([^-].*)$")
_ENV_STYLE_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ATTACHED_USER = re.compile(r"^-(u)([^-].*:.*)$")


def _words(name: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return [part for part in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if part]


def names_credential(name: str) -> bool:
    """True when a flag or an environment name has a credential word in it (a whole word, not a substring)."""
    return any(part in CREDENTIAL_FLAG_WORDS for part in _words(name)) or "".join(_words(name)) in CREDENTIAL_FLAG_NAMES


def _single_dash_names_credential(flag: str) -> bool:
    return flag[1:].lower() in CREDENTIAL_FLAG_WORDS


def _flag_takes_credential(flag: str, following: str) -> bool:
    if flag in VALUE_FLAGS:
        return True
    if flag.startswith("--"):
        return names_credential(flag[2:]) or (flag in USER_FLAGS and ":" in following)
    return (len(flag) == 2 and (flag[1] in CREDENTIAL_SHORT_FLAGS or (flag in USER_FLAGS and ":" in following))) or (
        len(flag) > 2 and _single_dash_names_credential(flag))


def _url_carries_credentials(arg: str) -> bool:
    if "://" not in arg:
        return False
    authority = arg.partition("://")[2].split("/", 1)[0]
    rest = arg.partition("://")[2]
    return "@" in authority or "?" in rest or "#" in rest


def argv_secret_slots(argv: list[Any]) -> dict[int, int]:
    """Index of each secret argument -> how many leading characters stay public (0: all of it is secret)."""
    slots: dict[int, int] = {}
    for index, arg in enumerate(argv):
        if not isinstance(arg, str):
            continue
        if "=" in arg and arg.startswith("-"):
            name, _, value = arg.partition("=")
            if value and (name in VALUE_FLAGS or names_credential(name) or (name in USER_FLAGS and ":" in value)):
                slots[index] = len(name) + 1
                continue
        elif "=" in arg:
            name, _, value = arg.partition("=")
            if value and names_credential(name):
                slots[index] = len(name) + 1
                continue
        elif arg.startswith("-"):
            following = argv[index + 1] if index + 1 < len(argv) else None
            # a value may begin with one dash (a password can); only another long flag ends the search
            if isinstance(following, str) and not following.startswith("--") and _flag_takes_credential(arg, following):
                slots[index + 1] = 0
            attached = _ATTACHED_SHORT.match(arg) or _ATTACHED_USER.match(arg)
            if attached and not _single_dash_names_credential(arg):
                slots[index] = 2
                continue
        elif _ENV_STYLE_NAME.match(arg) and names_credential(arg):
            # ``OPENAI_API_KEY value``: a variable name that names a credential, then its value
            following = argv[index + 1] if index + 1 < len(argv) else None
            if isinstance(following, str) and not following.startswith("-"):
                slots[index + 1] = 0
        if index not in slots and _url_carries_credentials(arg):
            slots[index] = 0
    return slots
