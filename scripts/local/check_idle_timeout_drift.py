"""Report ``TABBY_IDLE_TIMEOUT`` drift between ``start.sh`` and the systemd unit.

``start.sh`` is the source of truth for local-development defaults; the
systemd unit (``openai-proxy.service``) pins a value for the long-running
service. When they disagree, the service silently uses its own value and the
shell default has no effect, which has bitten the owner before.

This check is *read-only*: it never mutates ``openai-proxy.service`` or the
running unit. It prints a recommendation and exits non-zero on drift so it can
be wired into CI or a pre-deploy gate later.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_START_SH_PATTERN = re.compile(
    r"""TABBY_IDLE_TIMEOUT:?-?=?["']?(?P<value>\d+)["']?""",
)
_SERVICE_PATTERN = re.compile(
    r"""Environment\s*=\s*["']?TABBY_IDLE_TIMEOUT=(?P<value>\d+)["']?""",
)


def _read_value(path: Path, pattern: re.Pattern[str]) -> str | None:
    if not path.exists():
        return None
    text = path.read_text()
    match = pattern.search(text)
    if not match:
        return None
    return match.group("value")


def check_drift(
    start_sh: Path, service_unit: Path
) -> tuple[str | None, str | None, bool]:
    """Return ``(shell_value, service_value, drift)``.

    ``drift`` is ``True`` when both values are present and differ.
    """
    shell_value = _read_value(start_sh, _START_SH_PATTERN)
    service_value = _read_value(service_unit, _SERVICE_PATTERN)
    drift = (
        shell_value is not None
        and service_value is not None
        and shell_value != service_value
    )
    return shell_value, service_value, drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_idle_timeout_drift",
        description=(
            "Report TABBY_IDLE_TIMEOUT drift between start.sh and "
            "openai-proxy.service. Read-only; never mutates the unit."
        ),
    )
    parser.add_argument(
        "--start-sh",
        type=Path,
        default=Path("start.sh"),
        help="Path to start.sh (default: ./start.sh)",
    )
    parser.add_argument(
        "--service-unit",
        type=Path,
        default=Path("openai-proxy.service"),
        help="Path to the systemd unit file (default: ./openai-proxy.service)",
    )
    args = parser.parse_args(argv)

    shell_value, service_value, drift = check_drift(
        args.start_sh, args.service_unit
    )

    print(f"start.sh             TABBY_IDLE_TIMEOUT={shell_value or '<unset>'}")
    print(f"openai-proxy.service TABBY_IDLE_TIMEOUT={service_value or '<unset>'}")

    if shell_value is None or service_value is None:
        print("warning: could not read one or both values; skipping comparison")
        return 0

    if not drift:
        print("ok: values match")
        return 0

    print(
        "drift: start.sh and openai-proxy.service disagree on "
        "TABBY_IDLE_TIMEOUT."
    )
    print(
        f"recommendation: pick one value (suggest the larger: "
        f"{max(int(shell_value), int(service_value))}) and update both files. "
        "This check does not modify the unit or restart the service."
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
