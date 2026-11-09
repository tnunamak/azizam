"""Fresh-interpreter oracle: a gateway with an empty registry contacts no engine (S8 acceptance).

Run as ``python tests/support/fresh_gateway_oracle.py <scratch-dir> [--inject KIND]``. The DNS and outbound-socket
guards are installed before anything from the application is imported, so an import-time connection attempt is seen
even when the code under test swallows the error. The last stdout line is a JSON report.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ATTEMPTS: list[dict] = []


def _install_guards() -> None:
    def forbid(name, target=None):
        def guard(*args, **kwargs):
            where = ""
            if name in {"connect", "connect_ex", "sendto"}:
                where = repr(args[-1] if args else "")
            elif name in {"getaddrinfo", "gethostbyname", "gethostbyname_ex", "getnameinfo"}:
                where = repr(args[:2])
            elif name == "sendmsg":
                addr = args[3] if len(args) > 3 else kwargs.get("address")
                where = repr(addr)
            ATTEMPTS.append({"call": name, "to": where})
            raise OSError(f"network use is forbidden here: {name}")
        return guard

    for method in ("connect", "connect_ex", "sendto", "sendmsg"):
        setattr(socket.socket, method, forbid(method))
    for function in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "getnameinfo"):
        setattr(socket, function, forbid(function))


def _inject(kind: str) -> None:
    """Control: an import-time attempt whose error is swallowed. The guard must still have recorded it."""
    attempt = {
        "connect": lambda: socket.socket().connect(("203.0.113.1", 80)),
        "dns": lambda: socket.getaddrinfo("engine.invalid", 80),
        "gethostbyname": lambda: socket.gethostbyname("engine.invalid"),
        "connect_ex": lambda: socket.socket().connect_ex(("203.0.113.1", 80)),
        "gethostbyname_ex": lambda: socket.gethostbyname_ex("engine.invalid"),
        "sendmsg": lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendmsg([b"x"], [], 0, ("203.0.113.1", 53)),
        "udp": lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("203.0.113.1", 53)),
    }[kind]
    try:
        attempt()
    except OSError:
        pass


def main(argv: list[str]) -> int:
    scratch = Path(argv[1])
    inject = argv[argv.index("--inject") + 1] if "--inject" in argv else None
    preloaded = sorted(m for m in sys.modules if m.split(".")[0] in {"proxy", "gateway", "status_endpoint", "tests"})
    _install_guards()
    if inject:
        _inject(inject)
    sys.path.insert(0, str(ROOT))
    import proxy  # noqa: PLC0415 - must follow the guards
    from gateway import service_registry  # noqa: PLC0415
    from tests.support.adapter_differential_rig import GatewayRig  # noqa: PLC0415

    requests = [
        ("POST", "/v1/audio/speech", {"model": "tts-1", "input": "hi", "voice": "alloy"}),
        ("POST", "/v1/audio/generate", {"prompt": "a door creaks", "model": "tangoflux", "duration": 4}),
        ("POST", "/v1/images/generations", {"prompt": "a cat", "model": "image-fast"}),
        ("POST", "/v1/embeddings", {"model": "nomic-embed-text-v1.5", "input": "hi"}),
    ]
    hosts = ("TTS_HOST", "STT_HOST", "EMBEDDINGS_HOST", "COMFY_HOST")
    flags = ("TTS_ENABLED", "STT_ENABLED", "EMBEDDINGS_ENABLED", "COMFY_ENABLED")
    empty = proxy._derive_backend_constants({"backends": {}})
    report: dict = {"derived": {k: empty[k] for k in (*hosts, *flags)}, "statuses": [], "sent": []}
    with GatewayRig("fresh-no-egress", scratch) as rig:
        for key in (*hosts, *flags):
            rig.monkeypatch.setitem(proxy.runtime().derived, key, empty[key])
        for method, path, body in requests:
            response = asyncio.run(rig.call(method, path, label=f"fresh {path}", json_body=body))
            report["statuses"].append([path, response.status_code])
        report["sent"] = [call["to"] for call in rig.recorder.engine]
    report["released"] = service_registry.origin_of(
        service_registry.release_registry()["services"]["litellm"]["config"]["default_base_url"])
    report["attempts"] = ATTEMPTS
    report["preloaded_at_guard"] = preloaded
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
