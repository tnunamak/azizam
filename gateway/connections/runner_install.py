"""Verified release downloads for data-configured CLI runners. Never invokes a shell or installer."""
from __future__ import annotations

import asyncio
import hashlib
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx

from gateway.connections.cli_sign_in import bin_dir
from gateway.provider_connections import SubscriptionError

# The whole install. The binary is about 365 MB, so a 10 Mbit/s line needs about 5 minutes; a stalled read fails
# sooner, through the 60-second read timeout of each request.
INSTALL_SECONDS = 3600


def _download_message(name: str) -> str:
    return f"Azizam couldn't download {name}. Check the internet connection on the computer running Azizam, then try again."


def _platform_refusal(name: str) -> SubscriptionError:
    return SubscriptionError(400, "runner_install_platform", f"{name} doesn't support this computer's system.")


def platform_key(system: str, machine: str, libc: str, musl_file: bool, translated: bool = False) -> str | None:
    """The release platform name, or None when the release has none for this system."""
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine.lower())
    if system not in {"linux", "darwin", "win32"} or arch is None:
        return None
    if system == "darwin" and translated:
        arch = "arm64"
    suffix = "-musl" if system == "linux" and (libc == "musl" or (not libc and musl_file)) else ""
    return f"{system}-{arch}{suffix}"


def detect_platform() -> str | None:
    translated = False
    if sys.platform == "darwin" and platform.machine().lower() in {"x86_64", "amd64"}:
        try:
            translated = subprocess.run(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"],
                                        capture_output=True, timeout=2, check=False).stdout.strip() == b"1"
        except (OSError, subprocess.TimeoutExpired):
            pass
    musl_file = any(Path(f"/lib/libc.musl-{arch}.so.1").exists() for arch in ("x86_64", "aarch64"))
    return platform_key(sys.platform, platform.machine(), platform.libc_ver()[0], musl_file, translated)


async def download(kind, client_factory, progress=None) -> str:
    """Publish only complete, verified bytes; every failure or cancellation removes the temporary file.
    ``progress(received, size)`` hears each chunk, so the owner can see how far the download is."""
    spec = kind.row["runner"]["install"]
    name_of_program = str(spec.get("name") or kind.display_name)
    temporary = None
    try:
        async with asyncio.timeout(INSTALL_SECONDS):
            host = await asyncio.to_thread(detect_platform)
            if host is None:
                raise _platform_refusal(name_of_program)
            async with client_factory() as client:
                base = str(spec["base_url"]).rstrip("/")
                response = await client.get(f"{base}/{spec['channel']}", follow_redirects=True, timeout=30)
                response.raise_for_status()
                version = response.text.strip()
                if len(version) > 128 or not re.fullmatch(r"\d+\.\d+\.\d+(-\S+)?", version):
                    raise SubscriptionError(502, "runner_install_version", _download_message(name_of_program))
                values = {"version": quote(version, safe=""), "platform": host}
                response = await client.get(f"{base}/{spec['manifest_path'].format(**values)}", follow_redirects=True, timeout=30)
                response.raise_for_status()
                manifest = response.json()
                entry = manifest.get("platforms", {}).get(host)
                if not isinstance(entry, dict):
                    raise _platform_refusal(name_of_program)
                name, checksum, size = entry.get("binary"), entry.get("checksum"), entry.get("size")
                if (not isinstance(name, str) or not re.fullmatch(r"[\w.-]+", name) or name in {".", ".."}
                        or not isinstance(checksum, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", checksum)
                        or type(size) is not int or not 0 < size <= 1024 * 1024 * 1024):
                    raise SubscriptionError(502, "runner_install_manifest", _download_message(name_of_program))
                directory = bin_dir()
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                values["binary"] = name
                digest, received = hashlib.sha256(), 0
                with tempfile.NamedTemporaryFile(dir=directory, prefix=".runner-", delete=False) as output:
                    temporary = Path(output.name)
                    async with client.stream("GET", f"{base}/{spec['binary_path'].format(**values)}",
                                             follow_redirects=True, timeout=60) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes(64 * 1024):
                            received += len(chunk)
                            if progress is not None:
                                progress(received, size)
                            if received > size:
                                raise SubscriptionError(502, "runner_install_size", "The download was incomplete or damaged. Try again.")
                            digest.update(chunk)
                            output.write(chunk)
                if received != size:
                    raise SubscriptionError(502, "runner_install_size", "The download was incomplete or damaged. Try again.")
                if digest.hexdigest() != checksum.lower():
                    raise SubscriptionError(502, "runner_install_checksum", "The download could not be verified. Try again.")
                if sys.platform != "win32":
                    temporary.chmod(0o755)
                temporary.replace(directory / name)
                return version
    except SubscriptionError:
        raise
    except (httpx.HTTPError, TimeoutError, OSError, ValueError, KeyError, TypeError, AttributeError):
        raise SubscriptionError(502, "runner_install_download", _download_message(name_of_program)) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
