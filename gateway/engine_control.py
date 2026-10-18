"""Declared control operations for selection and residency.

Only an explicit operation row can cause a control request. This small runtime
keeps v2 control data separate from the legacy activation driver.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

import httpx

from gateway.engine_contract_view import connection_config, observations_config, residency_config
from gateway.engine_credentials import credential_headers
from gateway.engine_observations import ObservationReader
from gateway.engine_residency import ResidencyController
from gateway.engine_switch import SwitchController
from gateway.swap_drain import SwapDrain


class EngineControlRuntime:
    def __init__(self, service: Mapping[str, Any], *, drain: SwapDrain,
                 lock_for, observations: ObservationReader | None = None,
                 client_factory=httpx.AsyncClient) -> None:
        self.service = service
        self._drain = drain
        self._lock_for = lock_for
        self._observations = observations or ObservationReader()
        self._client_factory = client_factory

    def _operations(self) -> Mapping[str, Any]:
        config = self.service.get("config") or {}
        operations = self.service.get("protocol_operations") or config.get("protocol_operations")
        return operations if isinstance(operations, Mapping) else {}

    async def observe(self, name: str):
        return await self._observations.read({**self.service,
            "connection": connection_config(self.service),
            "observations": observations_config(self.service)}, name)

    async def invoke(self, name: str, args: Mapping[str, Any]) -> None:
        operation = self._operations().get(name)
        if not isinstance(operation, Mapping):
            raise ValueError(f"undeclared control operation: {name}")
        method = operation.get("method", "POST")
        path = operation.get("path")
        connection = connection_config(self.service)
        address = connection.get("address") if isinstance(connection, Mapping) else None
        if method not in {"GET", "POST", "PUT", "DELETE"} or not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("control operation needs an HTTP method and absolute path")
        if not isinstance(address, str) or urlsplit(address).scheme not in {"http", "https"}:
            raise ValueError("control operation needs an HTTP connection")
        url = urljoin(address.rstrip("/") + "/", path.lstrip("/"))
        origin = urlsplit(address)
        target = urlsplit(url)
        if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
            raise ValueError("control operation cannot cross origins")
        body, checked = self._check_paths(args, self._declared_path_arguments(operation))
        headers = credential_headers(self.service, "control")
        timeout_s = max(0.01, min(float(operation.get("timeout_s", 10)), 30))
        self._recheck_paths(checked)
        async with asyncio.timeout(timeout_s):
            async with self._client_factory(follow_redirects=False, trust_env=False) as client:
                async with client.stream(method, url, headers=headers,
                                         json=body if method != "GET" else None,
                                         timeout=timeout_s) as response:
                    if response.is_redirect or not 200 <= response.status_code < 300:
                        raise ValueError("control operation failed")
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 1048576:
                            raise ValueError("control response exceeded size limit")

    @staticmethod
    def _declared_path_arguments(operation: Mapping[str, Any]) -> frozenset[str]:
        """The operation's arguments declared ``{"type": "path"}``.

        That is the only declaration. Any other one is refused on every call, so
        a misspelt type cannot switch the path check off without a trace.
        """
        arguments = operation.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            raise ValueError("control operation arguments must be a mapping")
        for name, declaration in arguments.items():
            if not isinstance(declaration, Mapping) or declaration.get("type") != "path":
                raise ValueError(f"control argument {name} has an unsupported declaration; "
                                 "only type: path is supported")
        return frozenset(arguments)

    @staticmethod
    def _is_path_like(value: str) -> bool:
        """Whether the string's own form names a file, whatever argument carries it."""
        return (os.path.isabs(value) or value in {".", ".."} or value.startswith(("~", "./", "../"))
                or value.lower().startswith("file:"))

    def _check_paths(self, args: Mapping[str, Any], declared: frozenset[str]
                     ) -> tuple[Any, list[tuple[str, tuple[int, int] | None]]]:
        """Resolve every path argument under the owner's roots.

        An argument is a path when the operation declares it ``type: path``, and
        then every string under it is checked whatever its form. Elsewhere a
        string is a path only when its own form says so. Any other string is an
        identifier, such as ``org/model``, and passes unchanged. A path is
        checked against the roots even when relative, so a relative declared path
        is refused: a relative path means something only in the engine's working
        directory. Returns the arguments with each path replaced by its real
        path, plus the identities to re-check.
        """
        config = self.service.get("config") or {}
        roots = self.service.get("control_file_roots") or config.get("control_file_roots") or []
        if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
            raise ValueError("invalid control file roots")
        allowed = []
        for root in roots:
            expanded = Path(root).expanduser()
            if not expanded.is_absolute():
                raise ValueError("invalid control file roots: roots must be absolute")
            allowed.append(Path(os.path.realpath(expanded)))
        identities: list[tuple[str, tuple[int, int] | None]] = []

        def resolve(value: str) -> str:
            expanded = os.path.expanduser(value)
            outside = ValueError("control file path is outside owner allowlisted roots")
            if "\x00" in value or not os.path.isabs(expanded):
                raise outside
            real = Path(os.path.realpath(expanded))
            if not any(real.is_relative_to(root) for root in allowed):
                raise outside
            identities.append((str(real), self._identity(real)))
            return str(real)

        def walk(value: Any, as_path: bool) -> Any:
            if isinstance(value, Mapping):
                return {key: walk(item, as_path) for key, item in value.items()}
            if isinstance(value, list):
                return [walk(item, as_path) for item in value]
            if not isinstance(value, str):
                return value
            return resolve(value) if as_path or self._is_path_like(value) else value

        return {key: walk(item, key in declared) for key, item in args.items()}, identities

    @staticmethod
    def _identity(path: Path) -> tuple[int, int] | None:
        try:
            info = os.lstat(path)
        except OSError:
            return None
        return info.st_dev, info.st_ino

    def _recheck_paths(self, identities: list[tuple[str, tuple[int, int] | None]]) -> None:
        """Just before use: the same real path, the same file, and no symlink swapped in."""
        for real, expected in identities:
            changed = ValueError("control file path changed after it was checked")
            if os.path.realpath(real) != real:
                raise changed
            if expected is None:
                if self._identity(Path(real)) is not None:
                    raise changed
                continue
            try:
                # O_PATH | O_NOFOLLOW opens a symlink itself rather than its target.
                fd = os.open(real, getattr(os, "O_PATH", os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC)
            except OSError as exc:
                raise changed from exc
            try:
                info = os.fstat(fd)
            finally:
                os.close(fd)
            if stat.S_ISLNK(info.st_mode) or (info.st_dev, info.st_ino) != expected:
                raise changed

    async def select(self, offering_id: str, selection: Mapping[str, Any], *,
                     on_acquired=None, on_release=None, prepare=None):
        controller = SwitchController(drain=self._drain, invoke=self.invoke,
            observe=self.observe, lock_for=self._lock_for)
        return await controller.select(self.service, offering_id, selection,
                                       on_acquired=on_acquired, on_release=on_release,
                                       prepare=prepare)

    async def load(self) -> bool:
        controller = ResidencyController(invoke=self.invoke, observe=self.observe)
        return await controller.load({**self.service, "residency": residency_config(self.service)})

    async def unload(self) -> bool:
        controller = ResidencyController(invoke=self.invoke, observe=self.observe)
        return await controller.unload({**self.service, "residency": residency_config(self.service)})

    async def sleep(self) -> bool:
        controller = ResidencyController(invoke=self.invoke, observe=self.observe)
        return await controller.sleep({**self.service, "residency": residency_config(self.service)})

    async def wake(self) -> bool:
        controller = ResidencyController(invoke=self.invoke, observe=self.observe)
        return await controller.wake({**self.service, "residency": residency_config(self.service)})
